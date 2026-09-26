import pandas as pd
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, ACCOUNTS, ALIPAY_EXPENSE_MAP, get_payment_account, load_fund_map, save_fund_map, register_new_fund, sanitize_bean_df, is_backfill_period

# ──────────────────────────────────────────
# 路径
# ──────────────────────────────────────────
input_path       = f"整理后数据/{PERIOD}/支付宝整合_{PERIOD}.xlsx"
output_path      = f"bean_files/{PERIOD}/alipay_{PERIOD}.bean"
ride_output_path = f"bean_files/{PERIOD}/ride_{PERIOD}.bean"
account_path     = f"bean_files/account.bean"

# 元数据键名映射
META_MAP = {
    '交易时间': 'time',  '交易类型': 'type',   '交易对方': 'payee_raw',
    '商品':     'goods', '收/支':    'flow',    '当前状态': 'status',
    '备注':     'remark','数据来源': 'source',
}

# ──────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────
def _pay(text): return get_payment_account(str(text), source="alipay")

def _is_huabei_repay(payee, product) -> bool:
    """识别花呗还款，兼容新旧措辞：
    新版 "花呗自动还款-2023年02月账单" / 旧版 "自动还款-花呗2019年10月账单"、
    "主动还款-花呗…"。旧措辞不含"花呗自动还款"子串，会漏判落到 信用借还→Equity:Transfer，
    故对收款方为"花呗"且含"还款"的行一并识别。"""
    p = str(product)
    return "花呗自动还款" in p or (str(payee) == "花呗" and "还款" in p)

# 花呗自动还款拆分（银行卡&余额宝）：银行只扣其中一部分，余额从余额宝出。
# 银行 Excel（processor 已在本步骤前产出）里的「支付宝-还款」行给出银行实扣额。
_bank_repay_cache = None
def _bank_repay_amount(date, card4):
    """返回某日某卡（末4位）的「支付宝-还款」银行实扣总额；查不到返回 None。"""
    global _bank_repay_cache
    if _bank_repay_cache is None:
        _bank_repay_cache = {}
        result_dir = os.path.join(str(Path(__file__).parent.parent), "整理后数据", PERIOD)
        for fn in (os.listdir(result_dir) if os.path.isdir(result_dir) else []):
            m = re.match(r"银行卡(\d{4})_.*\.xlsx$", fn)
            if not m:
                continue
            c4 = m.group(1)
            try:
                bdf = pd.read_excel(os.path.join(result_dir, fn))
            except Exception:
                continue
            tcol = next((c for c in bdf.columns if "交易时间" in c), None)
            acol = next((c for c in bdf.columns if "金额" in c), None)
            pcol = next((c for c in bdf.columns if "交易对方" in c), None)
            if not (tcol and acol and pcol):
                continue
            t = pd.to_datetime(bdf[tcol], errors="coerce")
            for i in range(len(bdf)):
                if pd.isna(t.iloc[i]) or "还款" not in str(bdf[pcol].iloc[i]):
                    continue
                try:
                    amt = abs(float(bdf[acol].iloc[i]))
                except (TypeError, ValueError):
                    continue
                _bank_repay_cache.setdefault((c4, t.iloc[i].date()), 0.0)
                _bank_repay_cache[(c4, t.iloc[i].date())] += amt
    return _bank_repay_cache.get((card4, date))


def _bank_dest(payment):
    """提现/转出到银行卡等场景：从「支付方式」解析真实到账银行卡。
    解析不出（unknown）时：回溯账期兜底 Equity:Transfer（银行卡侧已全量入账，
    不再扰动银行卡余额），其余账期兜底 1001。
    指向 1002/3001 的回溯腿也已在 get_payment_account 内改记 Equity:Transfer。"""
    acc = _pay(payment)
    if acc != ACCOUNTS["unknown_equity"]:
        return acc
    return ACCOUNTS["equity_transfer"] if is_backfill_period() else ACCOUNTS["bank_1001"]

def clean_link(text): return re.sub(r'[^A-Za-z0-9_-]', '_', text)

def get_meta_lines(row):
    lines = []
    for cn, en in META_MAP.items():
        val = str(row.get(cn, ""))
        val = val.replace('\r', ' ').replace('\n', ' ')
        val = ' '.join(val.split()).strip()
        if val and val not in ["nan", "/", ""]:
            lines.append(f'  {en}: "{val}"')
    return lines

def get_fund_code(fund_name, row, fund_map, trade_date):
    if fund_name in fund_map:
        return fund_map[fund_name]
    raw_code = str(row.get("基金代码", "")).strip()
    if raw_code and raw_code != "nan":
        try:
            raw_code = str(int(float(raw_code))).zfill(6)
        except ValueError:
            pass
        return register_new_fund(fund_name, raw_code, fund_map, trade_date)
    print(f"[警告] 无法识别基金「{fund_name}」，请手动补充 fund_map.json")
    return None

# ──────────────────────────────────────────
# 主逻辑
# ──────────────────────────────────────────
fund_map = load_fund_map()
df_raw   = sanitize_bean_df(pd.read_excel(input_path)).fillna("")
excel_row_count = len(df_raw)

rows_in_bean = set()
rows_in_ride = set()
rows_filtered = set()

df = df_raw.copy()
df["交易时间"] = pd.to_datetime(df["交易时间"])
df = df.sort_values(by=["交易时间", "交易单号"]).reset_index()

for i, row in df.iterrows():
    keep    = (row["金额(元)"] != 0) | (row["支付方式"] == "哈啰骑行卡")
    status  = str(row.get("当前状态", ""))
    exclude_closed = (status == "交易关闭") & (row["收/支"] == "不计收支")
    exclude_failed = status == "还款失败"  # 花呗还款失败：未实际扣款，跳过
    if not keep or exclude_closed or exclude_failed:
        rows_filtered.add(row['index'])

bean_lines = []
ride_lines = []
skip_ids   = set()

# 退款预处理
for i, row in df.iterrows():
    orig_idx = row['index']
    if orig_idx in rows_filtered or "退款-" not in str(row["商品"]):
        continue

    product  = str(row["商品"])
    trade_id = str(row["交易单号"])
    keyword  = product.replace("退款-", "")
    date     = row["交易时间"].date()
    meta     = get_meta_lines(row)

    match = df[
        (df["商品"] == keyword) & (df["收/支"] == "支出") &
        (df["交易单号"] != trade_id) & (~df["商品"].str.startswith("退款-"))
    ]

    if not match.empty:
        original        = match.iloc[0]
        original_id     = str(original["交易单号"])
        orig_idx_match  = original['index']
        link_id         = clean_link(f"refund-{trade_id}")

        if original_id not in skip_ids:
            orig_meta = get_meta_lines(original)
            bean_lines.append(f'{original["交易时间"].date()} * "{original["交易对方"]}" "{original["商品"]}" ^{link_id}')
            bean_lines.extend(orig_meta)
            bean_lines.append(f'  {ALIPAY_EXPENSE_MAP.get(original["交易类型"], ACCOUNTS["unknown_expense"]):<55} {original["金额(元)"]:>10.2f} CNY\n  {_pay(original["支付方式"])}\n')

        bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}" ^{link_id}')
        bean_lines.extend(meta)
        bean_lines.append(f'  {_pay(row["支付方式"]):<55} {row["金额(元)"]:>10.2f} CNY\n  {ALIPAY_EXPENSE_MAP.get(original["交易类型"], ACCOUNTS["unknown_expense"])}\n')

        skip_ids.update([trade_id, original_id])
        rows_in_bean.update([orig_idx, orig_idx_match])
    else:
        bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
        bean_lines.extend(meta)
        bean_lines.append(f'  {_pay(row["支付方式"]):<55} {row["金额(元)"]:>10.2f} CNY\n  {ACCOUNTS["income_refund"]}\n')
        skip_ids.add(trade_id)
        rows_in_bean.add(orig_idx)

# 主循环
for i, row in df.iterrows():
    orig_idx = row['index']
    trade_id = str(row["交易单号"])
    if trade_id in skip_ids or orig_idx in rows_filtered:
        continue

    date    = row["交易时间"].date()
    amount  = row["金额(元)"]
    product = str(row["商品"])
    payment = str(row["支付方式"])
    meta    = get_meta_lines(row)

    # 骑行卡
    if amount == 0 and payment == "哈啰骑行卡":
        ride_lines.append(f'{date} * "Ride"')
        ride_lines.extend(meta)
        ride_lines.append(f'  {ACCOUNTS["ride"]:<55} 1 RIDE\n  {ACCOUNTS["equity_ride"]:<55} -1 RIDE\n')
        rows_in_ride.add(orig_idx)
        continue

    # 提现 - 实时提现（交易类型: 账户存取）
    # 余额 → 银行卡
    if str(row["交易类型"]) == "账户存取" and "提现" in product:
        bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
        bean_lines.extend(meta)
        bean_lines.append(f'  {_bank_dest(payment):<55} {amount:>10.2f} CNY\n  {ACCOUNTS["alipay_yue"]}\n')
        rows_in_bean.add(orig_idx)
        continue

    # 投资理财
    if str(row["交易类型"]) == "投资理财":
        # 余额宝 → 银行卡
        if product == "余额宝-转出到银行卡":
            bean_lines.append(f'{date} * "示例银行A" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {_bank_dest(payment):<55} {amount:>10.2f} CNY\n  {ACCOUNTS["alipay_yuebao"]}\n')
        # 余额宝 → 余额（支付宝内部资金调整）
        elif "余额宝-转出到余额" in product or "余额宝转出到余额" in product:
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {ACCOUNTS["alipay_yuebao"]:<55} {-amount:>10.2f} CNY\n  {ACCOUNTS["alipay_yue"]:<55} {amount:>10.2f} CNY\n')
        # 基金现金分红（至余额宝 / 至银行卡 / 至余额）
        elif "现金分红" in product:
            if "至银行卡" in product:
                cash_acc = _bank_dest(payment)
            elif "至余额" in product and "余额宝" not in product:
                cash_acc = ACCOUNTS["alipay_yue"]
            else:
                # 默认到余额宝（也覆盖 "至余额宝" 情形）
                cash_acc = ACCOUNTS["alipay_yuebao"]
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {cash_acc:<55} {amount:>10.2f} CNY\n  {ACCOUNTS["income_invest"]:<55} {-amount:>10.2f} CNY\n')
        # 余额宝等货币基金每日收益
        elif "收益发放" in product:
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {ACCOUNTS["alipay_yuebao"]:<55} {amount:>10.2f} CNY\n  {ACCOUNTS["income_invest"]}\n')
        # 基金买入 / 卖出 / 卖出到余额宝 / 等 → Pending 中转
        # bean_edit_alipay_funds.py 会在「确认」事件时把 Pending 平到具体基金账户
        elif "买入" in product:
            pay_acc = _pay(payment)
            if pay_acc == ACCOUNTS["unknown_equity"]:
                pay_acc = ACCOUNTS["alipay_yuebao"]  # 兜底：默认余额宝
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {ACCOUNTS["fund_pending"]:<55} {amount:>10.2f} CNY\n  {pay_acc:<55} {-amount:>10.2f} CNY\n')
        elif "卖出" in product:
            # 卖出回笼到余额宝 / 余额 / 银行卡
            if "至银行卡" in product:
                cash_acc = _bank_dest(payment)
            elif "至余额" in product and "余额宝" not in product:
                cash_acc = ACCOUNTS["alipay_yue"]
            else:
                cash_acc = ACCOUNTS["alipay_yuebao"]
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {cash_acc:<55} {amount:>10.2f} CNY\n  {ACCOUNTS["fund_sale_pending"]:<55} {-amount:>10.2f} CNY\n')
        # 转入余额宝（单次转入 / 自动转入 / 更换货基转入）：资金从银行卡/余额进入余额宝
        elif "余额宝" in product and "转入" in product:
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {ACCOUNTS["alipay_yuebao"]:<55} {amount:>10.2f} CNY\n  {_bank_dest(payment):<55} {-amount:>10.2f} CNY\n')
        # 转账收款到余额宝：他人转入，计内部转账中转
        elif "转账收款到余额宝" in product:
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {ACCOUNTS["alipay_yuebao"]:<55} {amount:>10.2f} CNY\n  {ACCOUNTS["equity_transfer"]:<55} {-amount:>10.2f} CNY\n')
        # 红包奖励发放到余额宝 → Income:Luck
        elif "红包奖励" in product:
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {ACCOUNTS["alipay_yuebao"]:<55} {amount:>10.2f} CNY\n  {ACCOUNTS["income_luck"]:<55} {-amount:>10.2f} CNY\n')
        else:
            # 未知投资理财类型，生成占位条目
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(f'  {ACCOUNTS["unknown_equity"]:<55} {amount:>10.2f} CNY\n  {ACCOUNTS["unknown_equity"]}\n')
        rows_in_bean.add(orig_idx)
        continue

    # 收入流水（转账红包 / 收钱码收款 / 收款等）：资金流入资产账户，符号与支出相反
    # —— 资产账户 +amount，对端账户 -amount。对端用 ALIPAY_EXPENSE_MAP 映射（转账红包→
    # Equity:Transfer），未映射的默认 Income:Unknown，交由 reclassifier 按 payee/goods 细化
    # （reclassifier 只改账户名、不动符号，故细化成 Expenses:X 时即为"负支出=报销"，正确）。
    if str(row["收/支"]) == "收入":
        asset_acc = _pay(payment)
        if asset_acc == ACCOUNTS["unknown_equity"]:
            asset_acc = ACCOUNTS["alipay_yue"]   # 收款支付方式常为空，默认入余额
        counter = ALIPAY_EXPENSE_MAP.get(str(row["交易类型"]), ACCOUNTS["unknown_income"])
        bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
        bean_lines.extend(meta)
        bean_lines.append(f'  {asset_acc:<55} {amount:>10.2f} CNY\n  {counter:<55} {-amount:>10.2f} CNY\n')
        rows_in_bean.add(orig_idx)
        continue

    # 花呗自动还款（拆分扣款：银行卡&余额宝）——银行只扣一部分，其余从余额宝出。
    # 支付方式如 "示例银行A储蓄卡(1001)&余额宝"：用银行 Excel 的「支付宝-还款」实扣额
    # 作为银行腿，差额记余额宝，避免把全额误记到银行卡（否则银行运行余额对不上）。
    card_m = re.search(r"\((\d{4})\)", payment)
    if _is_huabei_repay(row["交易对方"], product) and "&" in payment and "余额宝" in payment and card_m:
        bank_acc = _pay(payment)
        bank_portion = _bank_repay_amount(date, card_m.group(1))
        if bank_acc.startswith("Assets:Cash:Bank") and bank_portion is not None and 0 < bank_portion < amount:
            yue_portion = round(amount - bank_portion, 2)
            bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
            bean_lines.extend(meta)
            bean_lines.append(
                f'  {ACCOUNTS["huabei"]:<55} {amount:>10.2f} CNY\n'
                f'  {bank_acc:<55} {-bank_portion:>10.2f} CNY\n'
                f'  {ACCOUNTS["alipay_yuebao"]:<55} {-yue_portion:>10.2f} CNY\n'
            )
            rows_in_bean.add(orig_idx)
            continue

    # 普通消费 / 花呗还款
    target_acc = ACCOUNTS["huabei"] if _is_huabei_repay(row["交易对方"], product) else ALIPAY_EXPENSE_MAP.get(str(row["交易类型"]), ACCOUNTS["unknown_expense"])
    bean_lines.append(f'{date} * "{row["交易对方"]}" "{product}"')
    bean_lines.extend(meta)
    bean_lines.append(f'  {target_acc:<55} {amount:>10.2f} CNY\n  {_pay(payment)}\n')
    rows_in_bean.add(orig_idx)

# 审计 & 写文件
os.makedirs(f"bean_files/{PERIOD}", exist_ok=True)
count_bean   = len(rows_in_bean)
count_ride   = len(rows_in_ride)
count_filter = len(rows_filtered)
total_audit  = count_bean + count_ride + count_filter

audit_panel = (
    f"; ==================================================\n"
    f"; 支付宝 {PERIOD} 自动生成 - 审计面板\n"
    f"; 1. Excel 原始总行数:     {excel_row_count}\n"
    f"; 2. 骑行卡抵扣行:         {count_ride}\n"
    f"; 3. 被过滤行数(交易关闭): {count_filter}\n"
    f"; 4. 普通消费/退款/理财:   {count_bean}\n"
    f"; --------------------------------------------------\n"
    f"; [核对] {count_bean} + {count_ride} + {count_filter} = {total_audit}\n"
    f"; 状态: {'✅ 完美对应' if total_audit == excel_row_count else '❌ 存在差额'}\n"
    f"; ==================================================\n\n"
)

with open(output_path, "w", encoding="utf-8") as f:
    f.write(audit_panel)
    f.writelines("\n".join(bean_lines))

with open(ride_output_path, "w", encoding="utf-8") as f:
    f.write(f"; 哈啰骑行 {PERIOD} 自动生成\n; 本月骑行次数: {count_ride}\n\n")
    f.writelines("\n".join(ride_lines))

print(f"转换完成！勾稽结果: {total_audit}/{excel_row_count}")
if total_audit != excel_row_count:
    processed_all   = rows_in_bean | rows_in_ride | rows_filtered
    missing_indices = set(df_raw.index) - processed_all
    print(f"\n⚠️  [发现失踪行] 共有 {len(missing_indices)} 行未分配：")
    print(df_raw.loc[list(missing_indices), ["交易时间","商品","金额(元)","收/支"]].to_string())