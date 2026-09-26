#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bean 文件交易解析器（共享模块）
==============================
扫描 bean_files/<period>/*.bean，把每条 Expenses/Income/Equity 分录展开为
带源位置信息的 Txn 对象。供 suggest_rules.py / audit_labels.py 等工具复用。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).parent.parent
BEAN_ROOT = ROOT / "bean_files"

_TXN_START   = re.compile(r'^(\d{4}-\d{2}-\d{2})\s+[*!]')
_META_RE     = re.compile(r'^\s+(\w+):\s+"([^"]*)"')
_EXPENSE_RE  = re.compile(
    r'^\s+((?:Expenses|Income|Equity)(?::\w+)+)(?:\s+([-\d,\.]+)\s+(\w+))?'
)

_DEFAULT_SKIP_FILES = ("account.bean", "manual.bean", "main.bean")


@dataclass
class Txn:
    date: str
    payee: str           # 取自 meta.payee_raw，回退到 header 第一段引号
    goods: str
    type_: str
    source: str
    amount: float | None
    currency: str
    account: str         # Expenses:* / Income:* / Equity:*
    file: Path
    line: int            # 1-based，交易头所在行


def _parse_file(path: Path) -> Iterator[Txn]:
    lines = path.read_text(encoding="utf-8").splitlines()
    i = 0
    while i < len(lines):
        m = _TXN_START.match(lines[i])
        if not m:
            i += 1
            continue

        date_str = m.group(1)
        header_quotes = re.findall(r'"([^"]*)"', lines[i])
        header_payee = header_quotes[0] if header_quotes else ""

        block_start = i
        meta = {"payee_raw": header_payee, "goods": "", "type": "", "source": ""}
        postings: list[tuple[str, float | None, str]] = []
        j = i + 1
        while j < len(lines):
            ln = lines[j]
            if ln.strip() == "" or _TXN_START.match(ln) or re.match(r'^[a-zA-Z]', ln):
                break
            mm = _META_RE.match(ln)
            if mm:
                meta[mm.group(1)] = mm.group(2)
            ee = _EXPENSE_RE.match(ln)
            if ee:
                acct = ee.group(1)
                raw  = (ee.group(2) or "").replace(",", "")
                try:
                    amt = float(raw) if raw and raw not in ("-", ".") else None
                except ValueError:
                    amt = None
                cur  = ee.group(3) or ""
                postings.append((acct, amt, cur))
            j += 1

        for acct, amt, cur in postings:
            yield Txn(
                date=date_str,
                payee=meta.get("payee_raw") or header_payee,
                goods=meta.get("goods", ""),
                type_=meta.get("type", ""),
                source=meta.get("source", ""),
                amount=amt,
                currency=cur,
                account=acct,
                file=path,
                line=block_start + 1,
            )
        i = j


def iter_transactions(
    periods: list[str] | None = None,
    *,
    skip_files: tuple[str, ...] = _DEFAULT_SKIP_FILES,
    skip_funds: bool = True,
) -> Iterator[Txn]:
    """遍历 bean_files/ 下的账期目录，yield Txn。

    periods   : 限制扫描的账期目录名；None=全部
    skip_files: 跳过的文件名（默认账户/手工/主账本）
    skip_funds: 跳过基金分录（alipay_funds_*.bean），它们不参与分类
    """
    if periods is None:
        period_dirs = sorted(
            d for d in BEAN_ROOT.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    else:
        period_dirs = [BEAN_ROOT / p for p in periods]

    for d in period_dirs:
        if not d.exists():
            continue
        for fpath in sorted(d.glob("*.bean")):
            if fpath.name in skip_files or fpath.name.endswith(".bak"):
                continue
            if skip_funds and fpath.name.startswith("alipay_funds_"):
                continue
            yield from _parse_file(fpath)


if __name__ == "__main__":
    # 自检：打印各账户出现次数
    from collections import Counter
    c = Counter(t.account for t in iter_transactions())
    print(f"共扫描 {sum(c.values())} 条分录，{len(c)} 个不同账户")
    for acct, n in c.most_common(20):
        print(f"  {n:>5}  {acct}")
