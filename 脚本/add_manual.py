#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
手工记账助手
============
交互式命令行，快速往 manual.bean 追加一条分录。

用法:
    python 脚本/add_manual.py
    python 脚本/add_manual.py --date 2026-05-15
"""

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
MANUAL_BEAN = ROOT / "bean_files" / "manual.bean"

# ────────────────────────────────────────────────────────────
# 账户表
# ────────────────────────────────────────────────────────────

PAYMENT_ACCOUNTS = [
    ("示例银行A 储蓄卡", "Assets:Cash:Bank:BankA:Checking"),
    ("示例银行A 信用卡", "Liabilities:CreditCard:CNY"),
    ("支付宝余额",       "Assets:Cash:Alipay:Balance"),
    ("支付宝理财",       "Assets:Cash:Alipay:Savings"),
    ("微信余额",         "Assets:Cash:WeChat:Balance"),
    ("微信理财",         "Assets:Cash:WeChat:Savings"),
    ("消费信贷",         "Liabilities:ConsumerCredit"),
]

INCOME_ACCOUNTS = [
    ("工资",             "Income:Salary"),
    ("退款",             "Income:Refund"),
    ("投资收益",         "Income:Investment"),
    ("报销",             "Income:Reimbursement"),
    ("其他收入",         "Income:Other"),
    ("未分类收入",       "Income:Unknown"),
]

EXPENSE_ACCOUNTS = [
    ("餐饮",             "Expenses:Food"),
    ("购物",             "Expenses:Shopping"),
    ("交通",             "Expenses:Transport"),
    ("旅行",             "Expenses:Travel"),
    ("娱乐",             "Expenses:Entertainment"),
    ("美容",             "Expenses:Beauty"),
    ("健康",             "Expenses:Health"),
    ("教育",             "Expenses:Education"),
    ("服务",             "Expenses:Service"),
    ("保险",             "Expenses:Insurance"),
    ("手续费",           "Expenses:Fees"),
    ("税费",             "Expenses:Tax"),
    ("其他支出",         "Expenses:Other"),
    ("未分类支出",       "Expenses:Unknown"),
]


# ────────────────────────────────────────────────────────────
# 交互辅助
# ────────────────────────────────────────────────────────────

def _ask(prompt: str, default: str = "") -> str:
    """读取一行输入，支持默认值。"""
    hint = f"[{default}] " if default else ""
    try:
        raw = input(f"{prompt} {hint}").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。")
        sys.exit(0)
    return raw if raw else default


def _choose(prompt: str, options: list[tuple[str, str]]) -> str:
    """显示编号菜单，返回用户选择的账户字符串。
    输入 0 可手动输入任意账户名。
    """
    print(f"\n{prompt}")
    for i, (label, value) in enumerate(options, 1):
        print(f"  {i:>2}. {label:<22}  {value}")
    print(f"   0. 手动输入账户名")
    while True:
        raw = _ask("请选择编号:")
        if raw == "0":
            return _ask("账户名:").strip()
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(options):
                return options[idx][1]
        except ValueError:
            pass
        print("   输入无效，请重新输入。")


def _ask_amount() -> str:
    """读取金额，验证为正数，返回格式化字符串。"""
    while True:
        raw = _ask("金额 (CNY):")
        try:
            val = float(raw)
            if val <= 0:
                raise ValueError
            # 保留两位小数
            return f"{val:.2f}"
        except ValueError:
            print("   金额必须为正数，请重新输入。")


def _ask_date(default: str) -> str:
    """读取日期，格式 YYYY-MM-DD。"""
    while True:
        raw = _ask("日期 (YYYY-MM-DD):", default=default)
        try:
            datetime.strptime(raw, "%Y-%m-%d")
            return raw
        except ValueError:
            print("   日期格式错误，请使用 YYYY-MM-DD。")


# ────────────────────────────────────────────────────────────
# 分录生成
# ────────────────────────────────────────────────────────────

def _build_entry(txn_date: str, payee: str, narration: str,
                 debit_acct: str, credit_acct: str, amount: str,
                 currency: str = "CNY") -> str:
    """生成标准双行 beancount 分录。"""
    lines = [
        f'{txn_date} * "{payee}" "{narration}"',
        f"  {debit_acct:<47} {amount} {currency}",
        f"  {credit_acct}",
        "",
    ]
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────
# 记账流程
# ────────────────────────────────────────────────────────────

ENTRY_TYPES = [
    ("工资收入",            "salary"),
    ("报销到账",            "reimbursement"),
    ("支出",                "expense"),
    ("收入（非工资）",      "income"),
    ("内部转账 / 家庭收支", "transfer"),
    ("自定义（手动输入）",  "custom"),
]


def build_entry_interactive(default_date: str) -> str:
    print("\n" + "=" * 50)
    print("  手工记账助手")
    print("=" * 50)

    txn_date = _ask_date(default_date)

    print("\n记账类型:")
    for i, (label, _) in enumerate(ENTRY_TYPES, 1):
        print(f"  {i}. {label}")
    while True:
        raw = _ask("请选择类型:")
        try:
            etype = ENTRY_TYPES[int(raw) - 1][1]
            break
        except (ValueError, IndexError):
            print("   输入无效，请重新输入。")

    if etype == "salary":
        amount  = _ask_amount()
        payee   = _ask("单位名称:", default="公司")
        narr    = _ask("备注:", default=f"{txn_date[:7]} 工资")
        to_acct = _choose("到账账户:", PAYMENT_ACCOUNTS)
        entry = _build_entry(txn_date, payee, narr, to_acct, "Income:Salary", amount)

    elif etype == "reimbursement":
        amount  = _ask_amount()
        payee   = _ask("报销来源:", default="公司")
        narr    = _ask("备注:", default="报销到账")
        to_acct = _choose("到账账户:", PAYMENT_ACCOUNTS)
        inc_acct = _choose("报销收入账户:", INCOME_ACCOUNTS)
        entry = _build_entry(txn_date, payee, narr, to_acct, inc_acct, amount)

    elif etype == "expense":
        amount    = _ask_amount()
        payee     = _ask("商户 / 收款方:")
        narr      = _ask("备注:")
        exp_acct  = _choose("支出账户:", EXPENSE_ACCOUNTS)
        pay_acct  = _choose("支付账户:", PAYMENT_ACCOUNTS)
        entry = _build_entry(txn_date, payee, narr, exp_acct, pay_acct, amount)

    elif etype == "income":
        amount    = _ask_amount()
        payee     = _ask("来源:")
        narr      = _ask("备注:")
        inc_acct  = _choose("收入账户:", INCOME_ACCOUNTS)
        to_acct   = _choose("到账账户:", PAYMENT_ACCOUNTS)
        entry = _build_entry(txn_date, payee, narr, to_acct, inc_acct, amount)

    elif etype == "transfer":
        amount   = _ask_amount()
        payee    = _ask("对方 / 备注:", default="家庭")
        narr     = _ask("说明:")
        direction = _ask("方向（1=收入 / 2=支出）:", default="1")
        bank_acct = _choose("银行账户:", PAYMENT_ACCOUNTS)
        if direction == "2":
            entry = _build_entry(txn_date, payee, narr, "Equity:Internal", bank_acct, amount)
        else:
            entry = _build_entry(txn_date, payee, narr, bank_acct, "Equity:Internal", amount)

    else:  # custom
        payee    = _ask("Payee:")
        narr     = _ask("Narration:")
        amount   = _ask_amount()
        currency = _ask("货币:", default="CNY")
        debit    = _ask("借方账户 (debit):")
        credit   = _ask("贷方账户 (credit):")
        entry = _build_entry(txn_date, payee, narr, debit, credit, amount, currency)

    return entry


# ────────────────────────────────────────────────────────────
# 写入
# ────────────────────────────────────────────────────────────

def append_entry(entry: str) -> None:
    print("\n" + "-" * 50)
    print("预览：")
    print(entry)
    print("-" * 50)
    confirm = _ask("确认写入 manual.bean？(y/N):", default="N")
    if confirm.lower() != "y":
        print("已取消，未写入任何内容。")
        sys.exit(0)

    if not MANUAL_BEAN.exists():
        MANUAL_BEAN.parent.mkdir(parents=True, exist_ok=True)
        MANUAL_BEAN.write_text("", encoding="utf-8")

    with open(MANUAL_BEAN, "a", encoding="utf-8") as f:
        # 确保与前一条分录之间有空行
        existing = MANUAL_BEAN.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n\n"):
            f.write("\n")
        f.write(entry)

    print(f"✅ 已追加到 {MANUAL_BEAN.relative_to(ROOT)}")


# ────────────────────────────────────────────────────────────
# 入口
# ────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="手工记账助手 - 快速追加分录到 manual.bean")
    parser.add_argument("--date", default=None,
                        help="默认日期，格式 YYYY-MM-DD（缺省为今天）")
    args = parser.parse_args()

    default_date = args.date or date.today().strftime("%Y-%m-%d")
    entry = build_entry_interactive(default_date)
    append_entry(entry)


if __name__ == "__main__":
    main()
