#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
示例银行B PDF → 标准化 Excel
============================
解析「示例银行B交易流水*.pdf」并输出 `银行卡{card_id}_{PERIOD}.xlsx`，
列结构与 processor_bank.py 完全一致，可被 bean_edit_bank.py 直接消费。

PDF 文本特征：
  - 文件名前缀：「示例银行B」
  - 第一页含「账号：6217000000002001」标识完整卡号
  - 数据行格式：日期 货币 金额 联机余额 交易摘要 [对手信息]
  - 「对手信息」单元格常跨 1-3 行（名称上方/下方续行 + 账号在末尾）
"""

import os
import re
import sys
from collections import defaultdict
from pathlib import Path

import pandas as pd
import pdfplumber

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, validate_output, BANK_CARDS, find_card_by_full_account, find_raw_dir

BANK_NAME = "示例银行B"
CMB_FILENAME_PREFIX = "示例银行B"

STANDARD_COLUMNS = [
    '交易时间', '交易类型', '交易对方', '商品',
    '收/支', '金额(元)', '余额', '币别', '支付方式', '当前状态',
    '交易单号', '商户单号', '备注', '数据来源'
]

CURRENCY_MAP = {
    "人民币": "CNY",
    "CNY":    "CNY",
    "USD":    "USD",
    "EUR":    "EUR",
    "港币":   "HKD",
}

_ACCOUNT_PAT = re.compile(r"账号[：:]\s*([0-9]+)")
# 主数据行：日期 货币 金额 余额 类型 [可选对手内联]
_MAIN_PAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+(CNY|USD|EUR|HKD|人民币|美元|欧元|港币)"
    r"\s+(-?[\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})\s+(\S+?)(?:\s+(.+))?$"
)


def _extract_account_number(pdf) -> str | None:
    txt = pdf.pages[0].extract_text() or ""
    m = _ACCOUNT_PAT.search(txt)
    return m.group(1) if m else None


def _is_skip_line(line: str) -> bool:
    """识别页眉/页脚/分隔等需跳过的行。"""
    s = line.strip()
    if not s:
        return True
    if re.match(r"^\d+\s*/\s*\d+$", s):
        return True  # 页码 "1/3"
    if "—" in s or "─" in s:
        return True  # 分隔线
    if "温馨提示" in s or "交易流水验真" in s or "一网通首页" in s:
        return True
    if "示例银行B交易流水" in s or "Transaction Statement" in s:
        return True
    if "记账日期" in s and "对手信息" in s:
        return True
    if s in ("Transaction", "Amount"):
        return True
    if re.search(r"户\s*名", s):
        return True
    if "账户类型" in s or "Account Type" in s:
        return True
    if "申请时间" in s:
        return True
    if s.startswith("Name") or s.startswith("Date "):
        return True
    if "Sub" in s and "Branch" in s:
        return True
    if "Verification" in s:
        return True
    if re.match(r"^\d{4}-\d{2}-\d{2}\s+--\s+\d{4}-\d{2}-\d{2}$", s):
        return True  # 交易区间
    if re.match(r"账号[：:]", s):
        return True  # 账号信息行
    return False


def _classify_cont(line: str) -> str:
    """非日期行属于上一笔的「下续」还是下一笔的「上续」。

    规则：末尾是数字 → 下续（含账号）；否则 → 上续（名称片段）。
    """
    s = line.strip()
    return "below" if re.search(r"\d\s*$", s) else "above"


def _split_counterparty(text: str) -> tuple[str, str]:
    """拼接后的对手信息中分离 (名称, 账号)。账号 = 末尾连续数字段。"""
    s = text.strip()
    m = re.search(r"(\d+)\s*$", s)
    if m:
        return s[:m.start()].strip(), m.group(1)
    return s, ""


def _parse_pdf_text(pdf) -> list[dict]:
    """从 PDF 提取出每笔交易的结构化字典。"""
    txns: list[dict] = []
    current = None
    above_buf: list[str] = []
    below_buf: list[str] = []

    def _finalize():
        nonlocal current, above_buf, below_buf
        if current is None:
            return
        current["above_cont"] = above_buf
        current["below_cont"] = below_buf
        txns.append(current)
        current = None
        above_buf = []
        below_buf = []

    for page in pdf.pages:
        text = page.extract_text() or ""
        for line in text.splitlines():
            if _is_skip_line(line):
                continue
            m = _MAIN_PAT.match(line.strip())
            if m:
                _finalize()
                current = {
                    "date":        m.group(1),
                    "currency":    CURRENCY_MAP.get(m.group(2), m.group(2)),
                    "amount":      float(m.group(3).replace(",", "")),
                    "balance":     float(m.group(4).replace(",", "")),
                    "type":        m.group(5),
                    "inline_cp":   (m.group(6) or "").strip(),
                }
                # 将先前积累的「上续」并入新交易
                # （above_buf 已经在 _finalize 里清空，这里再次初始化为空列表是为了
                #  上一笔结束后到本笔之间收集到的 above-cont。但 above-cont 总是在
                #  current==None 时收集 → 见下分支。）
            else:
                cls = _classify_cont(line)
                if cls == "below" and current is not None and not below_buf:
                    below_buf.append(line.strip())
                else:
                    # 没有 current → 这是页首第一笔的上续
                    # 有 current 且 below_buf 非空 → 这是下一笔的上续
                    if current is None:
                        above_buf.append(line.strip())
                    else:
                        # 切换到「等待下一笔」：先 finalize 当前
                        # 但要保留这一行作为下一笔的 above-cont
                        pending = line.strip()
                        _finalize()
                        above_buf.append(pending)

    _finalize()
    return txns


def _assign_synthetic_times(txns: list[dict]) -> None:
    """同一天内按出现顺序赋予递增的伪时间，保持 PDF 排序。"""
    counter: dict[str, int] = defaultdict(int)
    for t in txns:
        idx = counter[t["date"]]
        counter[t["date"]] = idx + 1
        h, rem = divmod(idx, 3600)
        m, s = divmod(rem, 60)
        t["time"] = f"{t['date']} {h:02d}:{m:02d}:{s:02d}"


def _txn_to_row(t: dict, card_suffix: str) -> dict:
    full = "".join(t["above_cont"]) + t["inline_cp"] + "".join(t["below_cont"])
    name, account = _split_counterparty(full)
    amount = float(t["amount"])
    return {
        "交易时间":   t["time"],
        "交易类型":   t["type"],
        "交易对方":   name,
        "商品":       "",
        "收/支":      "收入" if amount > 0 else "支出",
        "金额(元)":   abs(amount),
        "余额":       float(t["balance"]),
        "币别":       t["currency"],
        "支付方式":   t["type"],
        "当前状态":   "交易成功",
        "交易单号":   "",
        "商户单号":   account,
        "备注":       "",
        "数据来源":   BANK_NAME,
    }


def read_bankb_pdf(file_path):
    print(f"    [Step 1] 正在读取 PDF: {os.path.basename(file_path)}")
    try:
        with pdfplumber.open(file_path) as pdf:
            card_full_no = _extract_account_number(pdf)
            txns = _parse_pdf_text(pdf)
    except Exception as e:
        print(f"    ❌ PDF 读取失败: {e}")
        return None, None

    if not txns:
        print("    ❌ 未解析到任何交易")
        return None, card_full_no

    _assign_synthetic_times(txns)
    card_suffix = (card_full_no or "")[-4:] if card_full_no else "????"
    rows = [_txn_to_row(t, card_suffix) for t in txns]
    df = pd.DataFrame(rows, columns=STANDARD_COLUMNS)
    print(f"    [Step 2] 解析完成，有效交易记录: {len(df)} 条")
    return df, card_full_no


def main():
    ROOT_DIR       = Path(__file__).parent.parent
    BANK_INPUT_DIR = find_raw_dir("示例银行B", PERIOD)
    RESULT_DIR     = ROOT_DIR / "整理后数据" / PERIOD
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    if not BANK_INPUT_DIR or not BANK_INPUT_DIR.exists():
        print(f"⊘ 找不到输入目录: 原始数据/示例银行B/{PERIOD}")
        return

    pdf_files = sorted(
        f for f in os.listdir(BANK_INPUT_DIR)
        if f.lower().endswith('.pdf') and f.startswith(CMB_FILENAME_PREFIX)
    )
    if not pdf_files:
        print(f"⊘ 在 {BANK_INPUT_DIR} 下没找到以「{CMB_FILENAME_PREFIX}」开头的 PDF 文件，跳过")
        return

    ok = 0
    skipped = 0
    for fn in pdf_files:
        df_result, card_full_no = read_bankb_pdf(BANK_INPUT_DIR / fn)
        if df_result is None:
            print(f"  ❌ 处理失败: {fn}")
            skipped += 1
            continue

        card_id = find_card_by_full_account(card_full_no) if card_full_no else None
        if not card_id:
            print(f"  ⚠️  未识别卡号 ({card_full_no!r})，跳过: {fn}")
            print(f"     如是新卡，请在 config.BANK_CARDS 添加注册")
            skipped += 1
            continue

        out_file = RESULT_DIR / f"银行卡{card_id}_{PERIOD}.xlsx"
        df_result.to_excel(out_file, index=False)
        if not validate_output(str(out_file)):
            print(f"  ❌ 输出验证失败: {out_file}")
            skipped += 1
            continue
        print(f"  ✅ {fn} → 银行卡{card_id}_{PERIOD}.xlsx ({len(df_result)} 行)")
        ok += 1

    print(f"\n📊 共处理 {ok} 张卡，跳过 {skipped} 个 PDF")
    if ok == 0 and skipped > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
