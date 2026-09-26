#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
银行卡 Excel 转 Beancount

多卡 + 多币别支持：
  - 遍历 config.BANK_CARDS 中所有已注册卡，每张卡对应一个 Excel + 一个 .bean
  - CNY 主体：原有逻辑（跳过支付宝/微信/京东重复流水）
  - 外币行：
      * 购汇/结汇：同一时间戳的 CNY + FX 配对成跨币种 entry（@@ 价格转换）
      * 跨卡转账：「对方卡号」匹配本人名下其他注册卡 → 走 Equity:Transfer
      * 其余（转入/现金取款/无法识别的对方）→ FX 单边 entry，挂 Equity:Unknown
"""

import os
import sys
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    PERIOD, ACCOUNTS, BANK_CARDS,
    find_card_by_counterparty_account, is_backfill_period, sanitize_bean_df,
)

META_MAP = {
    '交易时间': 'time',  '交易类型': 'type',   '交易对方': 'payee_raw',
    '商品':     'goods', '收/支':    'flow',    '当前状态': 'status',
    '备注':     'remark','数据来源': 'source',
}

# CMB 交易摘要中标记「通过第三方平台扣款」的类型 →
# 已在 alipay/wechat/jingdong bean 中体现，bank bean 内跳过避免重复
_CMB_THIRD_PARTY_TYPES = {"快捷支付", "银联快捷支付", "快捷退款"}


def _classify_third_party(row, bank_tag: str) -> str | None:
    """识别第三方平台代扣行 → 返回 'alipay'/'wechat'/'jd'/None。

    BANK_A 把第三方平台名写在「交易对方」（如 "财付通-CHAGEE..."），靠文本匹配；
    CMB 只在「交易摘要」标注「快捷支付」等通用名，靠类型匹配（不再细分平台）。
    """
    peer  = str(row.get('交易对方', ''))
    goods = str(row.get('商品', ''))
    tx_type = str(row.get('交易类型', '')).strip()
    text  = peer + goods

    if bank_tag in ("banka", "bankc"):
        # banka：跳过避免重复（见 skip 块）；bankc/回溯：用于把第三方对端腿改记 Equity:Transfer
        if "支付宝" in text:
            return "alipay"
        if "财付通" in text or "微信" in text:
            return "wechat"
        if "网银在线" in text or "京东" in text:
            return "jd"
        return None

    if bank_tag == "bankb":
        if tx_type in _CMB_THIRD_PARTY_TYPES:
            return "third_party"  # CMB 不区分具体平台
        return None

    return None


def _meta_lines(row) -> list[str]:
    out = []
    for cn, en in META_MAP.items():
        val = str(row.get(cn, ""))
        val = val.replace('\r', ' ').replace('\n', ' ')
        val = ' '.join(val.split()).strip()
        if val and val not in ["nan", ""]:
            out.append(f'  {en}: "{val}"')
    return out


def _process_card(card_id: str, input_path: Path, output_path: Path) -> bool:
    """处理单张卡的 Excel，生成对应 bean。返回是否成功。"""
    card = BANK_CARDS[card_id]
    cny_account = card["cny_account"]
    fx_accounts = card["fx_accounts"]
    bank_tag    = card["bank_tag"]

    CC_9001   = ACCOUNTS["credit_9001"]
    SALARY    = ACCOUNTS["income_salary"]
    MT_YUEFU  = ACCOUNTS["meituan_yuefu"]
    UNKNOWN   = ACCOUNTS["unknown_equity"]
    TRANSFER  = ACCOUNTS["equity_transfer"]
    INCOME_FX = ACCOUNTS["income_fx"]

    df_raw = sanitize_bean_df(pd.read_excel(input_path))
    excel_row_count = len(df_raw)
    if "币别" not in df_raw.columns:
        df_raw["币别"] = "CNY"

    rows_alipay      = set()
    rows_wechat      = set()
    rows_jd          = set()
    rows_third_party = set()  # CMB 等不区分平台的通用第三方跳过
    rows_yuebao_skip = set()
    rows_processed   = set()  # 单边 CNY entry
    rows_fx_solo     = set()  # 单边 FX entry，对端 Equity:Unknown
    rows_paired      = set()  # 同卡购汇/结汇 跨币种配对
    rows_xfer        = set()  # 跨卡内部转账（对端 Equity:Transfer）

    df = df_raw.fillna({"币别": "CNY"}).fillna("")
    df['交易时间'] = pd.to_datetime(df['交易时间'])
    df = df.sort_values(by='交易时间').reset_index()

    lines = []

    # ── 探测同卡内 购汇/结汇 跨币种配对
    pair_map: dict[int, int] = {}
    pair_type: dict[int, str] = {}
    grouped = df.groupby(["交易时间", "交易类型"])
    for (_t, _type), g in grouped:
        if _type not in ("购汇", "结汇") or len(g) != 2:
            continue
        cny_rows = g[g["币别"] == "CNY"]
        fx_rows  = g[g["币别"] != "CNY"]
        if len(cny_rows) != 1 or len(fx_rows) != 1:
            continue
        cny_row = cny_rows.iloc[0]
        fx_row  = fx_rows.iloc[0]
        if _type == "购汇" and not (cny_row["收/支"] == "支出" and fx_row["收/支"] == "收入"):
            continue
        if _type == "结汇" and not (cny_row["收/支"] == "收入" and fx_row["收/支"] == "支出"):
            continue
        pair_map[cny_row["index"]] = fx_row["index"]
        pair_map[fx_row["index"]]  = cny_row["index"]
        pair_type[cny_row["index"]] = _type
        pair_type[fx_row["index"]]  = _type

    for _, row in df.iterrows():
        orig_idx = row['index']
        currency = str(row['币别']) or "CNY"
        peer  = str(row['交易对方'])
        goods = str(row['商品'])
        text  = peer + goods
        counterparty_acc = str(row.get('商户单号', '')).strip()  # 对方卡号/账号

        # ── CNY 行：第三方平台流水跳过
        # bankc（示例银行C）回溯卡 + 回溯账期：不做任何跳过，第三方流水无法可靠对应
        # 支付宝/微信账单，全部记在银行卡侧，保证运行余额与 PDF 一致。
        no_skip = (bank_tag == "bankc") or is_backfill_period()
        if currency == "CNY" and not no_skip:
            if "余额宝转出到卡" in goods or ("蚂蚁" in peer and "基金销售" in peer and "余额宝" in goods and "转出" in goods):
                rows_yuebao_skip.add(orig_idx); continue
            tp = _classify_third_party(row, bank_tag)
            if tp == "alipay":
                rows_alipay.add(orig_idx); continue
            if tp == "wechat":
                rows_wechat.add(orig_idx); continue
            if tp == "jd":
                rows_jd.add(orig_idx); continue
            if tp == "third_party":
                rows_third_party.add(orig_idx); continue

        # ── 跨卡转账识别：对方卡号匹配本人名下另一张注册卡
        peer_card = find_card_by_counterparty_account(counterparty_acc)
        is_internal_xfer = (peer_card is not None and peer_card != card_id and "示例用户" in peer)

        # ── 同卡内购汇/结汇配对：由 CNY 侧负责生成
        if orig_idx in pair_map and not is_internal_xfer:
            if orig_idx in rows_paired:
                continue
            if currency != "CNY":
                continue  # 等遍历到 CNY 侧
            fx_orig = pair_map[orig_idx]
            fx_row  = df_raw.loc[fx_orig]
            fx_ccy  = str(fx_row["币别"])
            fx_account = fx_accounts.get(fx_ccy, UNKNOWN)
            cny_acct = cny_account or UNKNOWN

            t = pair_type[orig_idx]
            pay_date   = row['交易时间'].strftime('%Y-%m-%d')
            cny_amount = float(row['金额(元)'])
            fx_amount  = float(fx_row['金额(元)'])
            entry = [f'{pay_date} * "{peer}" "{t}"']
            entry.extend(_meta_lines(row))
            if t == "购汇":
                entry.append(f'  {cny_acct:<55} {-cny_amount:>10.2f} CNY')
                entry.append(f'  {fx_account:<55} {fx_amount:>10.2f} {fx_ccy} @@ {cny_amount:.2f} CNY')
            else:
                entry.append(f'  {fx_account:<55} {-fx_amount:>10.2f} {fx_ccy} @@ {cny_amount:.2f} CNY')
                entry.append(f'  {cny_acct:<55} {cny_amount:>10.2f} CNY')
                entry.append(f'  {INCOME_FX}')
            lines.append("\n".join(entry) + "\n")
            rows_paired.add(orig_idx)
            rows_paired.add(fx_orig)
            continue

        # ── 单边 FX 行
        if currency != "CNY":
            fx_account = fx_accounts.get(currency, UNKNOWN)
            pay_date  = row['交易时间'].strftime('%Y-%m-%d')
            amount    = float(row['金额(元)'])
            direction = str(row['收/支'])
            tx_type   = str(row['交易类型'])
            desc      = str(row['商品']) if row['商品'] != "" else tx_type
            sign      = amount if direction == "收入" else -amount

            # 信用卡 9001 跨账户来往（如 USD 转入/还款）→ 走 9001 的对应币别子账户
            cc_9001_acc = None
            if "示例用户" in peer and counterparty_acc == "6227000000009001":
                cc_9001_acc = "Liabilities:CreditCard:9001:USD" if currency == "USD" else CC_9001

            entry = [f'{pay_date} * "{peer}" "{desc}"']
            entry.extend(_meta_lines(row))
            entry.append(f'  {fx_account:<55} {sign:>10.2f} {currency}')
            if cc_9001_acc:
                entry.append(f'  {cc_9001_acc:<55} {-sign:>10.2f} {currency}')
                rows_processed.add(orig_idx)
            elif is_internal_xfer:
                entry.append(f'  {TRANSFER:<55} {-sign:>10.2f} {currency}')
                rows_xfer.add(orig_idx)
            else:
                entry.append(f'  {UNKNOWN}')
                rows_fx_solo.add(orig_idx)
            lines.append("\n".join(entry) + "\n")
            continue

        # ── 单边 CNY 行
        pay_date  = row['交易时间'].strftime('%Y-%m-%d')
        tx_type   = str(row['交易类型'])
        amount    = float(row['金额(元)'])
        direction = str(row['收/支'])
        desc      = str(row['商品']) if row['商品'] != "" else tx_type

        target = UNKNOWN
        if is_internal_xfer:
            target = TRANSFER
        elif "示例用户" in peer and counterparty_acc == "6227000000009001":
            target = CC_9001
        elif "示例机构" in peer and "补贴" in tx_type and direction == "收入":
            target = SALARY
        elif "美团" in text and "月付" in text and "还款" in text:
            target = MT_YUEFU
        elif "结息" in tx_type or "利息" in tx_type:
            target = ACCOUNTS["income_invest"]          # 银行结息 → 利息收入
        elif ("短信" in tx_type and "服务费" in tx_type) or "工本费" in text or "收费" in tx_type:
            target = "Expenses:Service:Fees"            # 银行短信费 / 开卡工本费 / 收费
        elif no_skip:
            # 回溯账期：银行流水全量入账，第三方/余额宝划转的对端记 Equity:Transfer，
            # 与支付宝/微信侧（同样改记 Transfer）对冲，避免落 Equity:Unknown。
            tp = _classify_third_party(row, bank_tag)
            is_yueb = any(k in text for k in ("余额宝", "天弘基金", "过渡账户", "代收付业务过渡"))
            if tp or is_yueb:
                target = TRANSFER

        cny_acct = cny_account or UNKNOWN
        amt_signed = amount if direction == "收入" else -amount
        entry = [f'{pay_date} * "{peer}" "{desc}"']
        entry.extend(_meta_lines(row))
        entry.append(f'  {cny_acct:<55} {amt_signed:>10.2f} CNY')
        if is_internal_xfer:
            entry.append(f'  {target:<55} {-amt_signed:>10.2f} CNY')
            rows_xfer.add(orig_idx)
        else:
            entry.append(f'  {target}')
            rows_processed.add(orig_idx)

        lines.append("\n".join(entry) + "\n")

    count_ali     = len(rows_alipay)
    count_we      = len(rows_wechat)
    count_jd      = len(rows_jd)
    count_tp      = len(rows_third_party)
    count_yueb    = len(rows_yuebao_skip)
    count_done    = len(rows_processed)
    count_fx_solo = len(rows_fx_solo)
    count_paired  = len(rows_paired)
    count_xfer    = len(rows_xfer)
    total = (count_ali + count_we + count_jd + count_tp + count_yueb +
             count_done + count_fx_solo + count_paired + count_xfer)

    audit_header = (
        f"; ==================================================\n"
        f"; {bank_tag.upper()} {card_id} 银行账单 - 审计面板\n"
        f"; 1. Excel 原始总行数:     {excel_row_count}\n"
        f"; 2. 转换为 Bean 条目:\n"
        f";     - 单边 CNY:           {count_done}\n"
        f";     - 单边 FX:            {count_fx_solo}\n"
        f";     - 跨币种配对 (行数):  {count_paired}\n"
        f";     - 跨卡转账:           {count_xfer}\n"
        f"; 3. 忽略（避免重复流水）:\n"
        f";     - 支付宝:             {count_ali}\n"
        f";     - 微信:               {count_we}\n"
        f";     - 京东:               {count_jd}\n"
        f";     - 第三方平台(笼统):   {count_tp}\n"
        f";     - 余额宝转出到卡:     {count_yueb}\n"
        f"; --------------------------------------------------\n"
        f"; [核对] {count_done}+{count_fx_solo}+{count_paired}+{count_xfer}+{count_ali}+{count_we}+{count_jd}+{count_tp}+{count_yueb} = {total}\n"
        f"; 状态: {'✅ 完美对应' if total == excel_row_count else '❌ 存在差额'}\n"
        f"; ==================================================\n\n"
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(audit_header)
        f.write("\n".join(lines))

    print(f"  ✅ {card_id}: {total}/{excel_row_count} → {output_path.name}")
    if total != excel_row_count:
        handled = (rows_processed | rows_alipay | rows_wechat | rows_jd | rows_third_party
                   | rows_yuebao_skip | rows_fx_solo | rows_paired | rows_xfer)
        missing = set(df_raw.index) - handled
        print(f"  ⚠️  失踪 {len(missing)} 行：\n{df_raw.loc[list(missing)].to_string()}")
        return False
    return True


def main():
    ROOT_DIR = Path(__file__).parent.parent
    bean_dir = ROOT_DIR / "bean_files" / PERIOD
    excel_dir = ROOT_DIR / "整理后数据" / PERIOD

    processed = 0
    failed = 0
    for card_id, card in BANK_CARDS.items():
        bank_tag = card["bank_tag"]
        input_path = excel_dir / f"银行卡{card_id}_{PERIOD}.xlsx"
        if not input_path.exists():
            print(f"  ⊘ {card_id}: 无 Excel ({input_path.name})，跳过")
            continue
        output_path = bean_dir / f"bank_{bank_tag}_{card_id}_{PERIOD}.bean"
        if _process_card(card_id, input_path, output_path):
            processed += 1
        else:
            failed += 1

    print(f"\n📊 共处理 {processed} 张卡，失败 {failed}")
    if failed > 0:
        sys.exit(1)


convert_to_beancount = main

if __name__ == "__main__":
    main()
