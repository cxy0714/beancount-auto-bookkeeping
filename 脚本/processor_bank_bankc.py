#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
示例银行C PDF → 标准化 Excel
============================
解析「示例银行C个人活期账户全部交易明细」PDF，输出 `银行卡{card_id}_{PERIOD}.xlsx`，
列结构与 processor_bank.py / processor_bank_bankb.py 完全一致，可被 bean_edit_bank.py 直接消费。

PDF 特征（pdfplumber 可直接抽表）：
  - 第一页含「卡号/账号:6217000000003001」标识完整卡号
  - 表头：序号 | 摘要 | 交易日期 | 交易金额 | 账户余额 | 交易地点/附言 | 对方账号与户名
  - 交易日期格式 YYYYMMDD；交易金额带正负号；账户余额为运行余额
  - 单个 PDF 跨多个年度（1902-2301 共 4 个年度账期），按 PERIOD 日期区间切片输出
"""

import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import pdfplumber

sys.path.insert(0, str(Path(__file__).parent))
from config import (
    PERIOD, validate_output, BANK_CARDS,
    find_card_by_full_account, find_raw_dir, period_date_bounds,
)

BANK_NAME = "示例银行C"

STANDARD_COLUMNS = [
    '交易时间', '交易类型', '交易对方', '商品',
    '收/支', '金额(元)', '余额', '币别', '支付方式', '当前状态',
    '交易单号', '商户单号', '备注', '数据来源'
]

_ACCOUNT_PAT = re.compile(r"卡号/账号[：:]\s*([0-9]+)")
_TABLE_HEADER = {"序号", "摘要", "交易日期", "交易金额", "账户余额"}


def _extract_account_number(pdf) -> str | None:
    txt = pdf.pages[0].extract_text() or ""
    m = _ACCOUNT_PAT.search(txt)
    return m.group(1) if m else None


def _clean(v) -> str:
    s = "" if v is None else str(v)
    return " ".join(s.replace("\n", " ").split()).strip()


def _split_counterparty(text: str) -> tuple[str, str]:
    """「对方账号与户名」格式如 6217000000004001/示例商户 → (户名, 账号)。"""
    s = _clean(text)
    if not s:
        return "", ""
    if "/" in s:
        acc, _, name = s.partition("/")
        return name.strip(), acc.strip()
    return s, ""


def read_bankc_pdf(file_path):
    print(f"    [Step 1] 正在读取 PDF: {os.path.basename(file_path)}")
    rows = []
    try:
        with pdfplumber.open(file_path) as pdf:
            card_full_no = _extract_account_number(pdf)
            for page in pdf.pages:
                for table in page.extract_tables():
                    for r in table:
                        if not r:
                            continue
                        seq = _clean(r[0])
                        if not seq.isdigit():
                            continue  # 跳过表头/续行/页脚
                        rows.append(r)
    except Exception as e:
        print(f"    ❌ PDF 读取失败: {e}")
        return None, None

    if not rows:
        print("    ❌ 未解析到任何交易")
        return None, card_full_no

    card_suffix = (card_full_no or "")[-4:] if card_full_no else "????"
    recs = []
    for r in rows:
        # 列：序号 摘要 交易日期 交易金额 账户余额 交易地点/附言 对方账号与户名
        summary  = _clean(r[1]) if len(r) > 1 else ""
        date_raw = _clean(r[2]) if len(r) > 2 else ""
        amt_raw  = _clean(r[3]) if len(r) > 3 else ""
        bal_raw  = _clean(r[4]) if len(r) > 4 else ""
        memo     = _clean(r[5]) if len(r) > 5 else ""
        cp_raw   = _clean(r[6]) if len(r) > 6 else ""

        m = re.match(r"(\d{4})(\d{2})(\d{2})", date_raw)
        if not m:
            continue
        date_iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

        try:
            amount = float(amt_raw.replace(",", ""))
        except ValueError:
            continue
        try:
            balance = float(bal_raw.replace(",", ""))
        except ValueError:
            balance = None

        name, account = _split_counterparty(cp_raw)
        recs.append({
            "date":    date_iso,
            "type":    summary,
            "payee":   name,
            "goods":   memo,
            "amount":  amount,
            "balance": balance,
            "account": account,
        })

    # 同一天内按出现顺序赋递增伪时间，保持 PDF 排序
    counter: dict[str, int] = defaultdict(int)
    out = []
    for rec in recs:
        idx = counter[rec["date"]]
        counter[rec["date"]] = idx + 1
        h, rem = divmod(idx, 3600)
        mi, se = divmod(rem, 60)
        out.append({
            "交易时间":  f"{rec['date']} {h:02d}:{mi:02d}:{se:02d}",
            "交易类型":  rec["type"],
            "交易对方":  rec["payee"],
            "商品":      rec["goods"],
            "收/支":     "收入" if rec["amount"] > 0 else "支出",
            "金额(元)":  abs(rec["amount"]),
            "余额":      rec["balance"],
            "币别":      "CNY",
            "支付方式":  rec["type"],
            "当前状态":  "交易成功",
            "交易单号":  "",
            "商户单号":  rec["account"],
            "备注":      "",
            "数据来源":  BANK_NAME,
        })

    df = pd.DataFrame(out, columns=STANDARD_COLUMNS)
    print(f"    [Step 2] 解析完成，全量交易记录: {len(df)} 条")
    return df, card_full_no


def main():
    ROOT_DIR       = Path(__file__).parent.parent
    BANK_INPUT_DIR = find_raw_dir("示例银行C", PERIOD)
    RESULT_DIR     = ROOT_DIR / "整理后数据" / PERIOD
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    if not BANK_INPUT_DIR or not BANK_INPUT_DIR.exists():
        print(f"⊘ 找不到示例银行C输入目录（覆盖 {PERIOD}），跳过")
        return

    pdf_files = sorted(f for f in os.listdir(BANK_INPUT_DIR) if f.lower().endswith(".pdf"))
    if not pdf_files:
        print(f"⊘ 在 {BANK_INPUT_DIR} 下没找到 PDF 文件，跳过")
        return

    # 合并对账单跨多年 → 按本账期日期区间切片
    try:
        start_iso, end_iso = period_date_bounds(PERIOD)
    except Exception:
        start_iso, end_iso = "0000-00-00", "9999-99-99"

    ok = 0
    skipped = 0
    for fn in pdf_files:
        df_all, card_full_no = read_bankc_pdf(BANK_INPUT_DIR / fn)
        if df_all is None:
            print(f"  ❌ 处理失败: {fn}")
            skipped += 1
            continue

        card_id = find_card_by_full_account(card_full_no) if card_full_no else None
        if not card_id:
            print(f"  ⚠️  未识别卡号 ({card_full_no!r})，跳过: {fn}")
            print(f"     如是新卡，请在 config.BANK_CARDS 添加注册")
            skipped += 1
            continue

        # 切片到本账期
        d = df_all["交易时间"].str.slice(0, 10)
        df_result = df_all[(d >= start_iso) & (d <= end_iso)].reset_index(drop=True)
        if len(df_result) == 0:
            print(f"  ⊘ {fn}: 本账期 [{start_iso}, {end_iso}] 无示例银行C交易，跳过")
            continue

        out_file = RESULT_DIR / f"银行卡{card_id}_{PERIOD}.xlsx"
        df_result.to_excel(out_file, index=False)
        if not validate_output(str(out_file)):
            print(f"  ❌ 输出验证失败: {out_file}")
            skipped += 1
            continue
        print(f"  ✅ {fn} → 银行卡{card_id}_{PERIOD}.xlsx "
              f"({len(df_result)}/{len(df_all)} 行，区间 {start_iso}~{end_iso})")
        ok += 1

    print(f"\n📊 共处理 {ok} 张卡，跳过 {skipped} 个 PDF")
    if ok == 0 and skipped > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
