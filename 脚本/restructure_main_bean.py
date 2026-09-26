#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.bean 结构改写工具
======================
用法：
  python 脚本/restructure_main_bean.py <PERIOD>   # 显式账期（可交互）
  python 脚本/restructure_main_bean.py            # 缺省取 config.PERIOD（非交互）

按 PERIOD 与 main.bean 现有账期区间的关系，自动判断三种模式：

  [ADD-END 模式] PERIOD 晚于现有最新账期（正向新账单 / 日常月度入账）
    → glob 该期 bean_files/{PERIOD}/*.bean，按来源优先级生成 include 块
    → 从银行 Excel 反推各月末 CNY 余额，生成 balance 断言
    → 追加到 main.bean 末尾（不动期初块，期初=上期期末已连续）
    → 跑 bean-check 自检并报告 balance 错误
    这是「跑完 pipeline 后把新一期挂进总账」的标准步骤，与回溯对称：
        python 脚本/pipeline.py 2606
        python 脚本/restructure_main_bean.py 2606

  [ADD-FRONT 模式] PERIOD 早于现有最早账期（新建回溯账期）
    → 计算新账期的 bank/fund/other 期初值
    → 把旧期初块改写为 balance 断言（旧日期 +1 天）
    → 插入新账期 include 行和新期初块

  [FIX 模式] PERIOD 已是 main.bean 的第一个账期，但初始化值不对
    → 运行 bean-check，根据期末 balance 错误反推正确的期初值，更新 main.bean

每次写入前自动备份 → bean_files/.backups/main.bean.bak.{timestamp}

注意：支付宝「账户余额」补充（bean_files/支付宝余额/alipay_yue_*.bean）是独立数据源，
单独 include、需人工复核去重，本工具不自动纳入。
"""

import re
import sys
import shutil
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).parent.parent
MAIN_BEAN = ROOT / "bean_files" / "main.bean"
BACKUP_DIR = ROOT / "bean_files" / ".backups"

sys.path.insert(0, str(Path(__file__).parent))


# ─────────────────────────────────────────────────────────
# 文本解析辅助
# ─────────────────────────────────────────────────────────

def _txn_block_end(lines: list[str], start_idx: int) -> int:
    """返回从 start_idx 开始的 beancount 事务块的结束索引（exclusive）。"""
    i = start_idx + 1
    while i < len(lines):
        line = lines[i]
        if line.strip() == "" or line.startswith(" ") or line.startswith("\t"):
            i += 1
        else:
            break
    while i > start_idx + 1 and lines[i - 1].strip() == "":
        i -= 1
    return i


def _find_first_init_idx(lines: list[str]) -> int:
    cash_pat = re.compile(r'^\d{4}-\d{2}-\d{2}\s+[*!]\s+"初始化"')
    for i, line in enumerate(lines):
        if cash_pat.match(line):
            return i
    raise ValueError("main.bean 中未找到 '初始化' 事务")


def _find_fund_init_idx(lines: list[str]) -> int:
    fund_pat = re.compile(r'^\d{4}-\d{2}-\d{2}\s+[*!]\s+"初始持仓回溯"')
    for i, line in enumerate(lines):
        if fund_pat.match(line):
            return i
    raise ValueError("main.bean 中未找到 '初始持仓回溯' 事务")


def _find_first_period_include_idx(lines: list[str]) -> int:
    """第一条带子目录路径的 include 行（即第一个账期 include）的行索引。"""
    period_inc = re.compile(r'^include\s+"[^"]+/[^"]+\.bean"')
    for i, line in enumerate(lines):
        if period_inc.match(line.strip()):
            return i
    raise ValueError("main.bean 中未找到账期 include 行")


def _parse_init_date(lines: list[str], start_idx: int) -> date:
    m = re.match(r'^(\d{4}-\d{2}-\d{2})', lines[start_idx])
    if not m:
        raise ValueError(f"无法从第 {start_idx+1} 行解析日期")
    return date.fromisoformat(m.group(1))


def _is_period_in_main(period: str) -> bool:
    return f'include "{period}/' in MAIN_BEAN.read_text(encoding="utf-8")


def _existing_periods_in_main(lines: list[str]) -> list[str]:
    """从 include 行解析 main.bean 中已存在的账期名（去重，保持出现顺序）。"""
    period_inc = re.compile(r'^include\s+"([^"/]+)/[^"]+\.bean"')
    periods: list[str] = []
    for line in lines:
        m = period_inc.match(line.strip())
        if m and m.group(1) not in periods:
            periods.append(m.group(1))
    return periods


# ─────────────────────────────────────────────────────────
# Beancount 错误解析（使用 Python API）
# ─────────────────────────────────────────────────────────

def _load_balance_errors(bean_path: Path) -> list[dict]:
    """
    加载 bean 文件并返回所有 BalanceError 的结构化信息：
      [{'date', 'account', 'expected', 'accumulated', 'diff', 'filename'}, ...]
    """
    import os
    cwd = os.getcwd()
    os.chdir(bean_path.parent)
    try:
        from beancount.loader import load_file
        entries, errors, _ = load_file(str(bean_path))
    finally:
        os.chdir(cwd)

    msg_pat = re.compile(
        r"Balance failed for '([^']+)': expected ([-\d,\.]+) (\w+) "
        r"!= accumulated ([-\d,\.]+) (\w+) "
        r"\(([-\d,\.]+) too (little|much)\)"
    )
    result = []
    for e in errors:
        if type(e).__name__ != "BalanceError":
            continue
        m = msg_pat.search(e.message or "")
        if not m:
            continue
        account, exp_s, exp_c, acc_s, _, diff_s, direction = m.groups()
        expected = Decimal(exp_s.replace(",", ""))
        accumulated = Decimal(acc_s.replace(",", ""))
        diff = expected - accumulated
        result.append({
            "date": e.entry.date,
            "account": account,
            "expected": expected,
            "accumulated": accumulated,
            "diff": diff,
            "currency": exp_c,
            "filename": e.source.get("filename", ""),
        })
    return result


# ─────────────────────────────────────────────────────────
# 文本片段生成
# ─────────────────────────────────────────────────────────

def _format_cash_init_block(init_date: date, account_values: dict[str, Decimal]) -> str:
    lines = [f'{init_date} * "初始化" "所有人民币账户期初余额"']
    for account, value in account_values.items():
        if account == "Equity:Opening-Balances":
            continue
        lines.append(f"  {account:<45} {value} CNY")
    lines.append(f"  {'Equity:Opening-Balances':<45}; 自动平衡")
    return "\n".join(lines)


def _format_balance_assertions(
    bal_date: date,
    cash_values: dict[str, Decimal],
    fund_values: dict[str, tuple[Decimal, str]],
) -> str:
    lines = [f"; ===== {bal_date} balance 断言（由 {bal_date - timedelta(days=1)} 期初转换）====="]
    for account, value in cash_values.items():
        if account == "Equity:Opening-Balances":
            continue
        lines.append(f"{bal_date} balance {account:<45} {value} CNY")
    for account, (units, fund_code) in sorted(fund_values.items()):
        lines.append(f"{bal_date} balance {account:<45} {units} {fund_code}")
    return "\n".join(lines)


def _period_include_block(period: str) -> str:
    """生成账期的 include 块：glob 该期目录下实际存在的 bean，按来源优先级排序。

    用前缀匹配而非写死文件名，确保信用卡（creditcard_banka_9001_*）、新增银行卡
    （bank_*）等都能被纳入，不会因命名变化而漏 include。
    """
    bean_dir = ROOT / "bean_files" / period
    if not bean_dir.exists():
        return ""
    # 来源前缀优先级（alipay_funds_ 必须排在 alipay_ 之前）
    order = ["ride_", "alipay_funds_", "alipay_", "wechat_",
             "jingdong_", "creditcard_", "bank_"]

    def sort_key(name: str):
        for i, pre in enumerate(order):
            if name.startswith(pre):
                return (i, name)
        return (len(order), name)

    files = sorted(
        (f.name for f in bean_dir.glob("*.bean") if not f.name.endswith(".bak")),
        key=sort_key,
    )
    return "\n".join(f'include "{period}/{fname}"' for fname in files)


def _format_monthly_bank_assertions(period: str) -> str:
    """从银行 Excel 提取各月最后一个交易日的 CNY 余额，生成 balance 断言。"""
    try:
        from read_bank_balance import get_daily_closing_balances, iter_card_currencies
        from config import BANK_CARDS
    except ImportError:
        return ""

    from collections import defaultdict
    # (year, month) → (date, balance, account) 取每月最后交易日
    monthly: dict[tuple, tuple] = {}
    for card_id, ccy in iter_card_currencies(period):
        if ccy != "CNY":
            continue
        card_info = BANK_CARDS.get(card_id, {})
        account = card_info.get("cny_account")
        if not account:
            continue
        for d, bal in get_daily_closing_balances(period, card_id, ccy):
            key = (d.year, d.month, account)
            if key not in monthly or d > monthly[key][0]:
                monthly[key] = (d, bal, account)

    if not monthly:
        return ""

    lines = ["; ===== 各月末银行余额断言（自动生成）====="]
    for (year, month, account) in sorted(monthly):
        d, bal, acct = monthly[(year, month, account)]
        # balance 断言校验「该日开始时」余额 = 前一日末余额，故用交易日 +1 天
        lines.append(f"{d + timedelta(days=1)} balance {acct:<45} {bal} CNY")
    return "\n".join(lines)


def _find_earliest_date_in_period(period: str) -> date | None:
    bean_dir = ROOT / "bean_files" / period
    earliest = None
    date_pat = re.compile(r'^(\d{4}-\d{2}-\d{2})\s+[*!]')
    for fpath in bean_dir.glob("*.bean"):
        with open(fpath, encoding="utf-8") as f:
            for line in f:
                m = date_pat.match(line)
                if m:
                    d = date.fromisoformat(m.group(1))
                    if earliest is None or d < earliest:
                        earliest = d
                    break
    return earliest


def _backup_and_write(content: str) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"main.bean.bak.{ts}"
    shutil.copy2(MAIN_BEAN, backup_path)
    print(f"  备份：{backup_path.relative_to(ROOT)}")
    MAIN_BEAN.write_text(content, encoding="utf-8")


# ─────────────────────────────────────────────────────────
# FIX 模式：修正已有账期的期初值
# ─────────────────────────────────────────────────────────

def _load_balance_errors_with_sidecar(period: str) -> list[dict]:
    """加载 main.bean + 银行 sidecar，返回所有 BalanceError。

    sidecar 临时生成，包含该期所有银行卡每日末余额断言；
    用以发现 main.bean 中未声明断言的银行卡（如新引入的示例银行B 2001）。
    """
    from read_bank_balance import get_daily_closing_balances, iter_card_currencies
    from config import BANK_CARDS
    bean_dir = ROOT / "bean_files"

    # 生成 sidecar 临时文件
    sidecar_lines = ['include "main.bean"\n']
    for card_id, ccy in iter_card_currencies(period):
        card = BANK_CARDS.get(card_id, {})
        if ccy == "CNY":
            acct = card.get("cny_account")
        else:
            acct = card.get("fx_accounts", {}).get(ccy)
        if not acct:
            continue
        for txn_date, balance in get_daily_closing_balances(period, card_id, ccy):
            assertion_date = txn_date + timedelta(days=1)
            sidecar_lines.append(
                f"{assertion_date} balance {acct:<50} {balance} {ccy}\n"
            )

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".bean", dir=str(bean_dir),
        delete=False, encoding="utf-8", prefix="_tmp_fix_"
    ) as f:
        f.writelines(sidecar_lines)
        tmp_path = Path(f.name)

    try:
        return _load_balance_errors(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def fix_init(period: str) -> None:
    """修正 main.bean 期初值。

    策略：
      - 银行卡账户（BANK_CARDS 中已注册的 cny_account）：直接使用 PDF 反推的「期初余额」
        覆盖，避免被「未分类的中间科目（Equity:Unknown）」污染。
      - 其它账户（YuEBao / LingQian / HuaBei …）：按 monthly 断言 errors 用 diff 增量调整。
    """
    from read_bank_balance import get_opening_balance
    from config import BANK_CARDS

    print(f"[FIX] 修正 {period} 的期初值…")

    lines = MAIN_BEAN.read_text(encoding="utf-8").splitlines(keepends=True)
    cash_init_idx = _find_first_init_idx(lines)
    cash_block_end = _txn_block_end(lines, cash_init_idx)
    new_lines = list(lines)

    # 解析现有 init 行的金额
    init_line_pat = re.compile(r'^\s+(\S+)\s+(-?[\d,\.]+)\s+CNY')
    old_inits: dict[str, Decimal] = {}
    for i in range(cash_init_idx + 1, cash_block_end):
        m = init_line_pat.match(new_lines[i])
        if m:
            old_inits[m.group(1)] = Decimal(m.group(2).replace(",", ""))

    # ── 第 1 步：银行卡 init 直接用 PDF 期初覆盖
    bank_changes: dict[str, Decimal] = {}
    for card_id, card in BANK_CARDS.items():
        acct = card.get("cny_account")
        if not acct:
            continue
        excel_path = ROOT / "整理后数据" / period / f"银行卡{card_id}_{period}.xlsx"
        if not excel_path.exists():
            continue
        opening = get_opening_balance(period, card_id, "CNY")
        if opening is None:
            continue
        old = old_inits.get(acct, Decimal("0"))
        if opening != old:
            bank_changes[acct] = opening
            print(f"  {acct}: 旧期初 {old} → 新期初 {opening} (来自 PDF)")

    # ── 第 2 步：非银行账户用 main.bean monthly 断言 errors 反推（增量调整）
    errors = _load_balance_errors(MAIN_BEAN)
    bank_acct_set = {card.get("cny_account") for card in BANK_CARDS.values() if card.get("cny_account")}

    cnY_errors_by_acct: dict[str, dict] = {}
    for e in errors:
        if e["currency"] != "CNY":
            continue
        acc = e["account"]
        if acc in bank_acct_set:
            continue  # 银行卡已经在上一步处理
        if not (acc.startswith("Assets:") or acc.startswith("Liabilities:")):
            continue
        if acc.startswith("Assets:Invest:Fund:"):
            continue
        prev = cnY_errors_by_acct.get(acc)
        if prev is None or e["date"] < prev["date"]:
            cnY_errors_by_acct[acc] = e

    other_changes: dict[str, Decimal] = {}
    for acc, info in cnY_errors_by_acct.items():
        old = old_inits.get(acc, Decimal("0"))
        new = old + info["diff"]
        other_changes[acc] = new
        print(f"  {acc}: 旧期初 {old} → 新期初 {new} (diff {info['diff']:+})")
        if acc.startswith("Assets:") and new < 0:
            print(f"  ⚠️  资产 {acc} 期初为负 ({new})，账单可能有误！")
        if acc.startswith("Liabilities:") and new > 0:
            print(f"  ⚠️  负债 {acc} 期初为正 ({new})，账单可能有误！")

    all_changes = {**bank_changes, **other_changes}
    if not all_changes:
        print("  ✅ 无需修正。")
        return

    # 写回：替换已有行 / 在 Equity:Opening-Balances 之前插入新行
    for acc, new_val in all_changes.items():
        replaced = False
        for i in range(cash_init_idx, cash_block_end):
            if acc in new_lines[i] and "CNY" in new_lines[i]:
                new_lines[i] = re.sub(
                    r'(  ' + re.escape(acc) + r'\s+)[-\d,\.]+(\s+CNY)',
                    lambda m, v=new_val: f"{m.group(1)}{v}{m.group(2)}",
                    new_lines[i]
                )
                replaced = True
                break
        if not replaced:
            for i in range(cash_init_idx, cash_block_end):
                if "Equity:Opening-Balances" in new_lines[i]:
                    new_lines.insert(i, f"  {acc:<45} {new_val} CNY\n")
                    cash_block_end += 1
                    break

    _backup_and_write("".join(new_lines))
    print("  ✅ main.bean 已更新。")


# ─────────────────────────────────────────────────────────
# ADD 模式：添加新的回溯账期
# ─────────────────────────────────────────────────────────

def add_period(period: str, interactive: bool = True) -> None:
    from read_bank_balance import get_opening_balance
    from back_fund import backcalc_holdings
    from beancount.loader import load_file
    from beancount.core.data import Transaction as Txn

    print(f"[ADD-FRONT] 添加回溯账期 {period}（插入 main.bean 前部）…")

    bean_dir = ROOT / "bean_files" / period
    if not bean_dir.exists():
        sys.exit(f"错误：找不到 bean_files/{period}/，请先运行 pipeline.py {period}")

    # 检查当前 main.bean 是否已有 balance 错误
    pre_errors = _load_balance_errors(MAIN_BEAN)
    if pre_errors:
        print(f"  ⚠️  当前 main.bean 已有 {len(pre_errors)} 个 balance 错误。")
        print(f"     建议先运行：python 脚本/restructure_main_bean.py <current_first_period>  修正")
        if interactive:
            ans = input("  是否继续？(y/N): ").strip().lower()
            if ans != "y":
                sys.exit(0)
        else:
            print("     非交互模式：继续执行。")

    lines = MAIN_BEAN.read_text(encoding="utf-8").splitlines(keepends=True)
    cash_init_idx = _find_first_init_idx(lines)
    fund_init_idx = _find_fund_init_idx(lines)
    first_period_inc_idx = _find_first_period_include_idx(lines)

    old_init_date = _parse_init_date(lines, cash_init_idx)
    balance_date = old_init_date + timedelta(days=1)

    # 1. 从 main.bean 加载旧期初值
    entries, _, _ = load_file(str(MAIN_BEAN))
    cash_inits = [e for e in entries if isinstance(e, Txn)
                  and e.payee == "初始化" and "所有人民币" in (e.narration or "")
                  and e.date == old_init_date]
    fund_inits = [e for e in entries if isinstance(e, Txn)
                  and "初始持仓回溯" in (e.narration or "")
                  and e.date == old_init_date]
    if not cash_inits:
        sys.exit(f"错误：未在 {old_init_date} 找到现金初始化事务")

    old_cash_init = cash_inits[0]
    old_cash_values: dict[str, Decimal] = {}
    for p in old_cash_init.postings:
        if p.account != "Equity:Opening-Balances":
            old_cash_values[p.account] = p.units.number if p.units else Decimal("0")

    old_fund_values: dict[str, tuple[Decimal, str]] = {}
    old_fund_holdings_text = ""
    if fund_inits:
        old_fund_init = fund_inits[0]
        for p in old_fund_init.postings:
            if p.units and hasattr(p.units, "currency") and p.units.currency.startswith("FUND"):
                old_fund_values[p.account] = (p.units.number, p.units.currency)
        fb_start = fund_init_idx
        fb_end = _txn_block_end(lines, fb_start)
        old_fund_holdings_text = "".join(lines[fb_start:fb_end])

    # 2. 新账期最早交易日 → 新期初日期
    new_earliest_date = _find_earliest_date_in_period(period)
    if new_earliest_date is None:
        sys.exit(f"错误：bean_files/{period}/ 中未找到任何交易")
    new_init_date = new_earliest_date - timedelta(days=1)
    print(f"  新账期最早交易日: {new_earliest_date} → 期初日期: {new_init_date}")

    # 3. 各银行卡期初（按 BANK_CARDS 遍历，只处理有 Excel 的卡）
    from config import BANK_CARDS
    bank_inits: dict[str, Decimal] = {}
    for card_id, card in BANK_CARDS.items():
        acct = card.get("cny_account")
        if not acct:
            continue
        excel_path = ROOT / "整理后数据" / period / f"银行卡{card_id}_{period}.xlsx"
        if not excel_path.exists():
            continue
        opening = get_opening_balance(period, card_id, "CNY")
        if opening is None:
            opening = Decimal("0")
        bank_inits[acct] = opening
        print(f"  银行期初：{acct} = {opening} CNY")
    if not bank_inits:
        sys.exit("错误：未找到任何银行 Excel，请先运行 pipeline.py")

    # 4. 基金期初（back_fund）
    fund_init_text = ""
    if old_fund_holdings_text:
        fund_init_text = backcalc_holdings(period, old_fund_holdings_text, str(new_init_date))
        print(f"  基金期初已反推完成")

    # 5. 其他账户期初（two-pass：先写 0，bean-check，再据 balance 错误反推）
    other_accts = [acc for acc in old_cash_values
                   if acc not in bank_inits and acc != "Equity:Opening-Balances"]

    # 临时现金 init：bank 正确，others=0
    tmp_cash_values = dict(bank_inits)
    for acc in other_accts:
        tmp_cash_values[acc] = Decimal("0")

    balance_assertions_text = _format_balance_assertions(
        balance_date, old_cash_values, old_fund_values
    )

    tmp_cash_block = _format_cash_init_block(new_init_date, tmp_cash_values)
    period_includes = _period_include_block(period)

    # FIX：只取真正的 header（旧 init 之前的部分），不包含旧 init 块
    header_text = "".join(lines[:cash_init_idx]).rstrip("\n")
    rest_text = "".join(lines[first_period_inc_idx:])

    tmp_content = (
        header_text + "\n\n"
        + tmp_cash_block + "\n\n"
        + fund_init_text + "\n\n"
        + period_includes + "\n\n"
        + balance_assertions_text + "\n\n"
        + rest_text
    )

    # 写到 bean_files/ 下，确保相对 include 能解析
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".bean", dir=str(ROOT / "bean_files"),
        delete=False, encoding="utf-8", prefix="_tmp_restructure_"
    ) as f:
        f.write(tmp_content)
        tmp_path = Path(f.name)

    try:
        print(f"  运行 bean-check 推导其他账户期初…")
        tmp_errors = _load_balance_errors(tmp_path)

        # 只关心 balance_date 当日的错误（属于 tmp 文件中的 balance 断言）
        other_inits: dict[str, Decimal] = {}
        sanity_warnings: list[str] = []

        for acc in other_accts:
            errs_at_balance_date = [
                e for e in tmp_errors
                if e["account"] == acc and e["date"] == balance_date
            ]
            if errs_at_balance_date:
                e = errs_at_balance_date[0]
                # correct_new_init = expected - accumulated = old_cash_value - delta_in_new_period
                new_init = e["diff"]
                other_inits[acc] = new_init
                msg = f"  {acc}: {new_init} CNY"
                if acc.startswith("Assets:") and new_init < 0:
                    msg += "  ⚠️ 资产为负，账单可能有错！"
                    sanity_warnings.append(f"{acc}: 资产为负 ({new_init})")
                if acc.startswith("Liabilities:") and new_init > 0:
                    msg += "  ⚠️ 负债为正，账单可能有错！"
                    sanity_warnings.append(f"{acc}: 负债为正 ({new_init})")
                print(msg)
            else:
                # 该账户在 balance_date 通过断言 → init=0 即可
                other_inits[acc] = Decimal("0")
                print(f"  {acc}: 0 CNY（已通过断言或无变动）")

        if sanity_warnings:
            print("\n  ⚠️  Sanity check 警告：")
            for w in sanity_warnings:
                print(f"      - {w}")
            print("  即使如此，将按反推结果写入。请运行 verify_period.py 进一步排查。\n")
    finally:
        tmp_path.unlink(missing_ok=True)

    # 6. 组装最终 main.bean
    final_cash_values = dict(bank_inits)
    for acc in other_accts:
        final_cash_values[acc] = other_inits[acc]

    final_cash_block = _format_cash_init_block(new_init_date, final_cash_values)
    final_balance_text = _format_balance_assertions(
        balance_date, old_cash_values, old_fund_values
    )
    monthly_assertions = _format_monthly_bank_assertions(period)

    final_content = (
        header_text + "\n\n"
        + final_cash_block + "\n\n"
        + fund_init_text + "\n\n"
        + period_includes + "\n\n"
        + (monthly_assertions + "\n\n" if monthly_assertions else "")
        + final_balance_text + "\n\n"
        + rest_text
    )

    _backup_and_write(final_content)
    print(f"  ✅ main.bean 已更新，新账期 {period} 期初日期：{new_init_date}")
    print(f"  下一步：python 脚本/verify_period.py {period}")


# ─────────────────────────────────────────────────────────
# ADD-END 模式：在 main.bean 末尾追加新账期（正向新账单）
# ─────────────────────────────────────────────────────────

def add_period_at_end(period: str) -> None:
    """新账单：把该期 include 块 + 各月末银行余额断言追加到 main.bean 末尾。

    与回溯（ADD-FRONT）不同，新账单的期初 = 上期期末（连续性已建立），
    故不改动期初块，只在文末 include + 加银行月末断言，然后跑 bean-check 自检。

    注意：支付宝「账户余额」补充文件（bean_files/支付宝余额/alipay_yue_*.bean）
    属独立数据源、单独 include，本步骤不自动纳入（其与 CSV 的去重需人工复核）。
    """
    print(f"[ADD-END] 在 main.bean 末尾追加新账期 {period}…")

    bean_dir = ROOT / "bean_files" / period
    if not bean_dir.exists():
        sys.exit(f"错误：找不到 bean_files/{period}/，请先运行 pipeline.py {period}")

    includes = _period_include_block(period)
    if not includes.strip():
        sys.exit(f"错误：bean_files/{period}/ 下没有可 include 的 bean 文件")

    assertions = _format_monthly_bank_assertions(period)

    content = MAIN_BEAN.read_text(encoding="utf-8")
    parts = [content.rstrip("\n"), "", includes]
    if assertions.strip():
        parts += ["", assertions]
    new_content = "\n".join(parts) + "\n"

    _backup_and_write(new_content)
    n_inc = len([l for l in includes.splitlines() if l.strip()])
    n_assert = len([l for l in assertions.splitlines() if l.strip().startswith("2") or "balance" in l]) if assertions else 0
    print(f"  ✅ main.bean 末尾已追加 {period}：{n_inc} 个 include + 银行月末断言")

    # ── 自检：跑 bean-check，报告所有 balance 错误（不阻断，供人工核对）
    print("  运行 bean-check 自检…")
    errors = _load_balance_errors(MAIN_BEAN)
    if not errors:
        print("  ✅ bean-check 通过，无 balance 错误。")
    else:
        print(f"  ⚠️  发现 {len(errors)} 个 balance 错误，请核对：")
        for e in errors:
            print(f"      {e['date']} {e['account']}: 期望 {e['expected']} "
                  f"≠ 实际 {e['accumulated']} (差 {e['diff']:+}) {e['currency']}")
        print("     （新账期常见原因：银行流水缺口；或对账校正.bean 的锚定断言"
              "落在本账期区间内被新数据 straddle，需相应调整。）")
    print(f"  下一步：python 脚本/verify_period.py {period}")


# ─────────────────────────────────────────────────────────
# 入口
# ─────────────────────────────────────────────────────────

def main() -> None:
    import os
    os.chdir(ROOT)

    # 显式传 PERIOD → 独立运行（可交互）；缺省 → 取 config.PERIOD（pipeline 调用，非交互）
    if len(sys.argv) >= 2:
        period = sys.argv[1]
        interactive = True
    else:
        from config import PERIOD as _CFG_PERIOD
        period = _CFG_PERIOD
        interactive = False
        print(f"（未指定账期，取 config.PERIOD = {period}）")

    from config import _period_code_bounds
    lines = MAIN_BEAN.read_text(encoding="utf-8").splitlines(keepends=True)
    existing = _existing_periods_in_main(lines)

    if _is_period_in_main(period):
        # FIX 仅对「第一个账期」有意义（反推首个初始化块）；其余已在 main 的账期
        # 视为已入账，幂等跳过（避免重复运行新账单入账时误触 FIX 破坏首个 init）。
        is_first = bool(existing) and period == existing[0]
        if is_first and interactive:
            print(f"检测到 {period} 是 main.bean 首个账期 → [FIX] 模式")
            fix_init(period)
        elif is_first:
            print(f"{period} 已在 main.bean 中，跳过结构改写"
                  f"（如需修正期初：python 脚本/restructure_main_bean.py {period}）")
        else:
            print(f"{period} 已在 main.bean 中（非首个账期），已入账，无需结构改写。")
        return

    # 不在 main 中：按账期编码区间决定插到前部（回溯）还是末尾（新账单）

    if not existing:
        print("检测到 main.bean 暂无账期 → [ADD-END] 模式")
        add_period_at_end(period)
        return

    p_start, p_end = _period_code_bounds(period)
    starts, ends = [], []
    for p in existing:
        try:
            s, e = _period_code_bounds(p)
        except ValueError:
            continue
        starts.append(s)
        ends.append(e)
    min_start, max_end = min(starts), max(ends)

    if p_end < min_start:
        print(f"检测到 {period} 早于现有最早账期 → [ADD-FRONT] 回溯模式")
        add_period(period, interactive=interactive)
    elif p_start > max_end:
        print(f"检测到 {period} 晚于现有最新账期 → [ADD-END] 新账单模式")
        add_period_at_end(period)
    else:
        sys.exit(
            f"⚠️  {period} 与现有账期区间 [{min_start}, {max_end}] 重叠但未被 include，"
            f"无法判定插入位置，请手动检查 main.bean。"
        )


if __name__ == "__main__":
    main()
