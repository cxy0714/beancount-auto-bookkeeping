#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账期 Balance 核对 + 错误定位工具
==================================
用法：
  python 脚本/verify_period.py <PERIOD>

功能：
  1. 对银行账户，提取每个交易日的「当日末余额」，生成 sidecar balance 断言
  2. 调 beancount Python API 加载 main.bean + sidecar，解析所有 balance 错误
  3. 对银行账户执行 delta-jump 算法：定位到具体哪一天引入了差额
  4. 拉出该日相关原始账单行，辅助人工排查
  5. 生成报告：reports/balance_audit_{PERIOD}.md

输出：
  - reports/balance_audit_{PERIOD}.md  （核对报告）
  - bean_files/balance_audit_{PERIOD}.bean  （sidecar，核对完后可删除）
"""

import os
import re
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent.parent
MAIN_BEAN = ROOT / "bean_files" / "main.bean"

sys.path.insert(0, str(Path(__file__).parent))
from config import BANK_CARDS
from read_bank_balance import get_daily_closing_balances, _get_bank_df, iter_card_currencies


# ─────────────────────────────────────────────────────────
# Sidecar 生成
# ─────────────────────────────────────────────────────────

def _build_bank_sidecar(period: str, sidecar_path: Path) -> list[tuple[str, str, date, Decimal]]:
    """生成多卡多币别的每日末余额断言。返回 [(card_id, currency, txn_date, balance)]."""
    lines = [
        f"; ==================================================",
        f"; Balance Audit Sidecar for {period}",
        f"; 银行每日末余额断言（多卡 + 多币别，verify_period.py 自动生成）",
        f"; ==================================================",
        "",
    ]
    flat: list[tuple[str, str, date, Decimal]] = []
    for card_id, ccy in iter_card_currencies(period):
        card = BANK_CARDS[card_id]
        if ccy == "CNY":
            acct = card.get("cny_account")
        else:
            acct = card["fx_accounts"].get(ccy)
        if not acct:
            lines.append(f"; ⚠️  跳过未注册 (card={card_id}, ccy={ccy})")
            continue
        lines.append(f"; ----- {card_id} / {ccy} → {acct} -----")
        for txn_date, balance in get_daily_closing_balances(period, card_id, ccy):
            assertion_date = txn_date + timedelta(days=1)
            lines.append(
                f"{assertion_date} balance {acct:<50} {balance} {ccy}"
                f"  ; {txn_date} 末余额"
            )
            flat.append((card_id, ccy, txn_date, balance))
        lines.append("")

    sidecar_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return flat


def _append_fund_sidecar(period: str, sidecar_path: Path) -> None:
    """追加基金份额每日 balance 断言。

    起点采用 main.bean 实际加载后、该 period 第一笔基金交易"之前"的余额，
    确保跨 period 累积正确（不再依赖单个 init 行）。
    """
    from beancount.parser import parser
    from beancount.loader import load_file
    from beancount.core.data import Transaction as Txn

    fund_bean = ROOT / "bean_files" / period / f"alipay_funds_{period}.bean"
    if not fund_bean.exists():
        return

    entries, _, _ = parser.parse_file(str(fund_bean))

    daily_deltas: dict[date, dict[str, Decimal]] = {}
    for e in entries:
        if not isinstance(e, Txn):
            continue
        for p in e.postings:
            try:
                if p.units and p.units.currency.startswith("FUND"):
                    daily_deltas.setdefault(e.date, {})
                    daily_deltas[e.date][p.units.currency] = (
                        daily_deltas[e.date].get(p.units.currency, Decimal("0"))
                        + p.units.number
                    )
            except (AttributeError, TypeError):
                pass

    if not daily_deltas:
        return

    # 起点 = 该 period 第一笔基金交易日的前一天的实际累积余额
    period_start = min(daily_deltas.keys())
    cutoff = period_start - timedelta(days=1)

    main_entries, _, _ = load_file(str(MAIN_BEAN))
    cumulative: dict[str, Decimal] = {}
    for e in main_entries:
        if not isinstance(e, Txn) or e.date > cutoff:
            continue
        for p in e.postings:
            try:
                if p.units and p.units.currency.startswith("FUND"):
                    cumulative[p.units.currency] = (
                        cumulative.get(p.units.currency, Decimal("0")) + p.units.number
                    )
            except (AttributeError, TypeError):
                pass

    out_lines = ["", "; 基金份额每日 balance 断言"]
    for d in sorted(daily_deltas):
        for fund_code in sorted(daily_deltas[d]):
            cumulative[fund_code] = cumulative.get(fund_code, Decimal("0")) + daily_deltas[d][fund_code]
            assertion_date = d + timedelta(days=1)
            acct = f"Assets:Invest:Fund:{fund_code}"
            out_lines.append(
                f"{assertion_date} balance {acct:<45} {cumulative[fund_code]} {fund_code}"
                f"  ; {d} 末份额"
            )

    with open(sidecar_path, "a", encoding="utf-8") as f:
        f.write("\n".join(out_lines) + "\n")


# ─────────────────────────────────────────────────────────
# Bean-check（Python API）
# ─────────────────────────────────────────────────────────

def _run_check_via_api(sidecar_path: Path) -> list[dict]:
    """
    生成临时 main，include 原 main.bean + sidecar，用 Python API 加载，
    解析 BalanceError，只保留 sidecar 中产生的错误。
    """
    bean_dir = ROOT / "bean_files"
    sidecar_rel = sidecar_path.relative_to(bean_dir)

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".bean", dir=str(bean_dir),
        delete=False, encoding="utf-8", prefix="_tmp_verify_"
    ) as f:
        f.write(f'include "main.bean"\n')
        f.write(f'include "{sidecar_rel}"\n')
        tmp_path = Path(f.name)

    cwd = os.getcwd()
    os.chdir(bean_dir)
    try:
        from beancount.loader import load_file
        _, errors, _ = load_file(str(tmp_path))
    finally:
        os.chdir(cwd)
        tmp_path.unlink(missing_ok=True)

    msg_pat = re.compile(
        r"Balance failed for '([^']+)': expected ([-\d,\.]+) (\w+) "
        r"!= accumulated ([-\d,\.]+) (\w+) "
        r"\(([-\d,\.]+) too (little|much)\)"
    )
    sidecar_abs = str(sidecar_path.resolve())
    result = []
    for e in errors:
        if type(e).__name__ != "BalanceError":
            continue
        if e.source.get("filename") != sidecar_abs:
            continue
        m = msg_pat.search(e.message or "")
        if not m:
            continue
        account, exp_s, exp_c, acc_s, _, _, _ = m.groups()
        expected = Decimal(exp_s.replace(",", ""))
        accumulated = Decimal(acc_s.replace(",", ""))
        result.append({
            "account": account,
            "bal_date": e.entry.date,
            "txn_date": e.entry.date - timedelta(days=1),
            "expected": expected,
            "accumulated": accumulated,
            "diff": expected - accumulated,  # 正 = 不足；负 = 超出
            "currency": exp_c,
        })
    return sorted(result, key=lambda x: (x["account"], x["bal_date"]))


# ─────────────────────────────────────────────────────────
# Delta-Jump 分析
# ─────────────────────────────────────────────────────────

def _delta_jump_analysis(errors: list[dict]) -> dict[str, list[dict]]:
    """
    对每个账户：按日期排序，找出每个新引入差额的日期段。
    返回 {account: [{date_range, delta, accumulated_diff, is_first}, ...]}
    """
    by_acct: dict[str, list[dict]] = {}
    for e in errors:
        by_acct.setdefault(e["account"], []).append(e)

    result = {}
    for account, acct_errors in by_acct.items():
        acct_errors.sort(key=lambda x: x["txn_date"])
        jumps = []
        prev_diff = Decimal("0")
        prev_date = None
        for e in acct_errors:
            delta = e["diff"] - prev_diff
            if delta != 0:
                jumps.append({
                    "date_range": (prev_date, e["txn_date"]),
                    "delta": delta,
                    "accumulated_diff": e["diff"],
                    "is_first": prev_diff == 0,
                })
            prev_diff = e["diff"]
            prev_date = e["txn_date"]
        result[account] = jumps

    return result


# ─────────────────────────────────────────────────────────
# 原始账单查询
# ─────────────────────────────────────────────────────────

def _get_bank_rows_for_date(period: str, target_date: date) -> list[str]:
    try:
        df = _get_bank_df(period)
        rows = df[df["交易时间"].dt.date == target_date]
        out = []
        for _, row in rows.iterrows():
            out.append(
                f"  {row['交易时间']} | {row['收/支']} | "
                f"¥{float(row['金额(元)']):>10.2f} | 余额 {float(row['余额']):>10.2f} | {row['交易对方']}"
            )
        return out
    except Exception as ex:
        return [f"  (读取银行 Excel 失败: {ex})"]


def _get_bean_entries_for_date_account(period: str, target_date: date, account: str) -> list[str]:
    bean_dir = ROOT / "bean_files" / period
    date_pat = re.compile(r'^(\d{4}-\d{2}-\d{2})\s+[*!]')
    results = []

    for fpath in sorted(bean_dir.glob("*.bean")):
        with open(fpath, encoding="utf-8") as f:
            lines = f.readlines()
        i = 0
        while i < len(lines):
            m = date_pat.match(lines[i])
            if m and date.fromisoformat(m.group(1)) == target_date:
                block = [lines[i]]
                j = i + 1
                while j < len(lines):
                    ln = lines[j]
                    if ln.strip() == "" or ln.startswith(" ") or ln.startswith("\t"):
                        block.append(ln)
                        j += 1
                    else:
                        break
                if account in "".join(block):
                    results.append(f"  [{fpath.name}]\n" + "".join("    " + l for l in block))
                i = j
                continue
            i += 1
    return results


# ─────────────────────────────────────────────────────────
# 报告生成
# ─────────────────────────────────────────────────────────

def _generate_report(
    period: str,
    errors: list[dict],
    jumps: dict[str, list[dict]],
    daily_ground_truth: list,
    report_path: Path,
) -> None:
    bank_accts = set()
    for card_id, card in BANK_CARDS.items():
        if card.get("cny_account"):
            bank_accts.add(card["cny_account"])
        for acct in card["fx_accounts"].values():
            bank_accts.add(acct)
    bank_errors = [e for e in errors if e["account"] in bank_accts]
    fund_errors = [e for e in errors if e["account"].startswith("Assets:Invest:Fund:")]

    lines = [
        f"# Balance 核对报告 — {period}",
        "",
        f"> 生成时间：{date.today()}  ·  工具：verify_period.py",
        "",
        "## 概要",
        "",
    ]
    if not errors:
        lines += [
            "✅ **所有 balance 断言通过，账单与银行记录吻合。**",
            "",
            "## 银行账户 balance 详情",
            "",
            f"✅ 银行 {len(daily_ground_truth)} 个交易日的余额断言全部通过。",
            "",
        ]
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  报告：{report_path.relative_to(ROOT)}")
        return

    accts_with_errors = sorted(set(e["account"] for e in errors))
    lines += [f"❌ 发现 {len(accts_with_errors)} 个账户存在 balance 差异。", ""]
    for acct in accts_with_errors:
        n = len([e for e in errors if e["account"] == acct])
        lines.append(f"- ❌ `{acct}` — {n} 个断言失败")
    lines.append("")

    # 银行详情 — 按 (账户) 分段
    if bank_errors:
        lines += ["## 银行账户 balance 详情", ""]
        for bank_acct in sorted(set(e["account"] for e in bank_errors)):
            acct_jumps = jumps.get(bank_acct, [])
            currency = next((e["currency"] for e in bank_errors if e["account"] == bank_acct), "CNY")
            lines += [
                f"### `{bank_acct}` ({currency})", "",
                "| 日期范围 | 新引入差额 | 累计差额 | 说明 |",
                "|---|---|---|---|",
            ]
            for j in acct_jumps:
                dfrom = j["date_range"][0] or "期初"
                dto = j["date_range"][1]
                note = "← **首个错误**" if j["is_first"] else ""
                lines.append(
                    f"| {dfrom} → {dto} | {j['delta']:+.2f} {currency} | "
                    f"{j['accumulated_diff']:+.2f} {currency} | {note} |"
                )
            lines += ["", "#### 需人工核查的日期", ""]
            for j in acct_jumps:
                if j["delta"] == 0:
                    continue
                dto = j["date_range"][1]
                sign_word = "少记/漏收" if j["delta"] > 0 else "多记/漏支"
                lines += [
                    f"##### 📅 {dto} — 差额 {j['delta']:+.2f} {currency}（{sign_word}）",
                    "",
                    f"**该日涉及该账户的 .bean 分录：**", "```",
                ]
                bean_entries = _get_bean_entries_for_date_account(period, dto, bank_acct)
                lines += bean_entries if bean_entries else ["  （无相关 .bean 分录）"]
                lines += ["```", ""]

    # 基金详情
    if fund_errors:
        lines += ["## 基金份额 balance 详情", ""]
        fund_accts = sorted(set(e["account"] for e in fund_errors))
        for acct in fund_accts:
            fund_errs = sorted(
                [e for e in fund_errors if e["account"] == acct],
                key=lambda x: x["txn_date"]
            )
            first = fund_errs[0]
            lines += [
                f"#### `{acct}`",
                f"- 首次出错日期：{first['txn_date']}",
                f"- 期望份额：{first['expected']}  实际：{first['accumulated']}  差：{first['diff']:+}",
                "",
            ]
            if acct in jumps:
                for j in jumps[acct]:
                    dfrom = j["date_range"][0] or "期初"
                    dto = j["date_range"][1]
                    lines.append(f"  - {dfrom} → {dto}：新差额 {j['delta']:+}")
                lines.append("")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"  报告：{report_path.relative_to(ROOT)}")


# ─────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────

def verify_period(period: str) -> bool:
    os.chdir(ROOT)

    excel_dir = ROOT / "整理后数据" / period
    any_excel = list(excel_dir.glob("银行卡*.xlsx")) if excel_dir.exists() else []
    if not any_excel:
        sys.exit(f"错误：找不到任何银行 Excel（{excel_dir}/银行卡*.xlsx）")

    sidecar_path = ROOT / "bean_files" / f"balance_audit_{period}.bean"
    report_path = ROOT / "reports" / f"balance_audit_{period}.md"

    print(f"[VERIFY] 账期：{period}")
    print(f"  生成 sidecar：{sidecar_path.relative_to(ROOT)}")

    daily = _build_bank_sidecar(period, sidecar_path)
    print(f"  银行 sidecar：{len(daily)} 条 (按卡+币别)")

    _append_fund_sidecar(period, sidecar_path)

    print(f"  运行 bean-check…")
    errors = _run_check_via_api(sidecar_path)

    if errors:
        print(f"  发现 {len(errors)} 个 balance 断言失败")
    else:
        print(f"  ✅ 所有 balance 断言通过！")

    jumps = _delta_jump_analysis(errors)

    bank_accts = set()
    for card in BANK_CARDS.values():
        if card.get("cny_account"):
            bank_accts.add(card["cny_account"])
        bank_accts.update(card["fx_accounts"].values())
    for acct in sorted(bank_accts):
        if acct in jumps and jumps[acct]:
            print(f"\n  🔍 {acct} delta-jump：")
            for j in jumps[acct]:
                dfrom = j["date_range"][0] or "期初"
                dto = j["date_range"][1]
                print(f"    {dfrom} → {dto}：差额 {j['delta']:+.2f}（累计 {j['accumulated_diff']:+.2f}）")

    _generate_report(period, errors, jumps, daily, report_path)
    return len(errors) == 0


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    ok = verify_period(sys.argv[1])
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
