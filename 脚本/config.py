#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账本全局配置
============
每次更新账期只改这一个文件里的 PERIOD。
账户名称以 bean_files/account.bean 为权威来源。
"""

import json
import os
from datetime import date as dt_date, timedelta
from pathlib import Path

_SCRIPT_DIR = Path(__file__).parent
_ROOT_DIR   = _SCRIPT_DIR.parent

# ============================================================
# ✏️  每月只改这里 ← ← ←
PERIOD = "2501"
# ============================================================

# 天天基金历史净值接口（{fund_code} 替换为 6 位数字代码）
# 说明：旧的 fundgz.1234567.com.cn 实时估值接口已下线（返回错误页），
#       改用 f10/lsjz 历史净值接口，取最近一条已确认净值（DWJZ/FSRQ）。
#       该接口必须带 Referer: http://fund.eastmoney.com/ 否则被拒。
FUND_NAV_API_URL = "https://api.fund.eastmoney.com/f10/lsjz?fundCode={fund_code}&pageIndex=1&pageSize=1"


# ────────────────────────────────────────────────────────────
# 共用账户（所有账单来源均可能涉及）
# ────────────────────────────────────────────────────────────
ACCOUNTS = {
    # 资产
    "bank_1001":        "Assets:Cash:Bank:BankA:Checking",
    "bank_1001_eur":    "Assets:Cash:Bank:BankA:EUR",
    "bank_1001_usd":    "Assets:Cash:Bank:BankA:USD",
    "bank_1002":        "Assets:Cash:Bank:BankA:Savings",
    "bank_1003":        "Assets:Cash:Bank:BankA:Foreign",
    "bank_1003_eur":    "Assets:Cash:Bank:BankA:Foreign:EUR",
    "bank_1003_usd":    "Assets:Cash:Bank:BankA:Foreign:USD",
    "bank_2001":        "Assets:Cash:Bank:BankB:Checking",
    "bank_3001":        "Assets:Cash:Bank:BankC:Checking",
    "alipay_yuebao":    "Assets:Cash:Alipay:YuEBao",
    "alipay_yue":       "Assets:Cash:Alipay:YuE",
    "wechat_lingqian":  "Assets:Cash:Wechat:LingQian",
    "wechat_lingqiantong": "Assets:Cash:Wechat:LingQianTong",
    "jd_xiaojinku":     "Assets:Cash:Jingdong:XiaoJinKu",
    "jd_yue":           "Assets:Cash:Jingdong:YuE",
    "ride":             "Assets:Prepaid:Transit",

    # 负债
    "credit_9001":      "Liabilities:CreditCard:CNY",
    "huabei":           "Liabilities:HuaBei",
    "jd_xianxianghoufu": "Liabilities:Jingdong:XianXiangHouFu",
    "jd_baitiao":       "Liabilities:Jingdong:BaiTiao",
    "meituan_yuefu":    "Liabilities:Meituan:Yuefu",

    # 收入
    "income_salary":    "Income:Salary",
    "income_refund":    "Income:Refund",
    "income_invest":    "Income:Investment",
    "income_luck":      "Income:Other",
    "income_fx":        "Income:Exchange",

    # 权益（占位）
    "equity_transfer":  "Equity:Transfer",
    "equity_opening":   "Equity:Opening-Balances",
    "equity_rounding":  "Equity:Rounding",
    "equity_family":    "Equity:Internal",
    "equity_ride":      "Equity:Prepaid",

    # 基金中转账户
    "fund_pending":      "Assets:Invest:Fund:Pending",
    "fund_sale_pending": "Assets:Invest:Fund:SalePending",
    "invest_fee":        "Expenses:Fees",

    # 未知占位（手动修正用）
    "unknown_expense":  "Expenses:Unknown",
    "unknown_income":   "Income:Unknown",
    "unknown_equity":   "Equity:Unknown",
}


# ────────────────────────────────────────────────────────────
# 银行卡注册表
# ────────────────────────────────────────────────────────────
# 每张卡用末 4 位标识。新增卡时只需在此追加一条。
# - full_account: PDF 头部「借记卡号」完整号码（用于识别 PDF 归属 + 跨卡转账检测）
# - bank_tag:     bean 文件名前缀（用于输出 bank_{tag}_{card_id}_{PERIOD}.bean）
# - cny_account:  本卡 CNY 主账户（不必每张卡都有 CNY；1003 不持 CNY 时留空）
# - fx_accounts:  本卡能持有的外币 → 对应账户
BANK_CARDS = {
    "1001": {
        "full_account": "6217000000001001",
        "bank_tag":     "banka",
        "cny_account":  ACCOUNTS["bank_1001"],
        "fx_accounts":  {
            "EUR": ACCOUNTS["bank_1001_eur"],
            "USD": ACCOUNTS["bank_1001_usd"],
        },
    },
    "1002": {
        "full_account": "6217000000001002",
        "bank_tag":     "banka",
        "cny_account":  ACCOUNTS["bank_1002"],
        "fx_accounts":  {},
    },
    "1003": {
        "full_account": "6217000000001003",
        "bank_tag":     "banka",
        "cny_account":  None,
        "fx_accounts":  {
            "EUR": ACCOUNTS["bank_1003_eur"],
            "USD": ACCOUNTS["bank_1003_usd"],
        },
    },
    "2001": {
        "full_account": "6217000000002001",
        "bank_tag":     "bankb",
        "cny_account":  ACCOUNTS["bank_2001"],
        "fx_accounts":  {},
    },
    "3001": {
        "full_account": "6217000000003001",
        "bank_tag":     "bankc",
        "cny_account":  ACCOUNTS["bank_3001"],
        "fx_accounts":  {},
    },
}


def find_card_by_full_account(full_account: str) -> str | None:
    """根据 PDF 头部「借记卡号」全号反查卡 id（末 4 位）。"""
    for card_id, info in BANK_CARDS.items():
        if info["full_account"] == full_account:
            return card_id
    # 兜底：按末 4 位匹配
    last4 = full_account[-4:] if full_account else ""
    return last4 if last4 in BANK_CARDS else None


def find_card_by_counterparty_account(account_no: str) -> str | None:
    """根据「对方卡号/账号」识别是否本人名下其他卡（用于跨卡转账去重）。"""
    if not account_no:
        return None
    acc = str(account_no).strip()
    for card_id, info in BANK_CARDS.items():
        if info["full_account"] in acc or acc in info["full_account"]:
            return card_id
    return None


# ────────────────────────────────────────────────────────────
# 支付方式关键词 → 账户
# 各账单来源的关键词有重叠，统一在此维护；
# 下方按来源分组仅用于注释说明，匹配时统一顺序执行。
# ────────────────────────────────────────────────────────────

# 通用：银行卡 / 信用卡（支付宝、微信、京东、银行均出现）
# 顺序敏感：先匹配末 4 位卡号（更具体），再回退到银行名通用规则
PAYMENT_RULES_COMMON = [
    ("1001",            ACCOUNTS["bank_1001"]),
    ("1002",            ACCOUNTS["bank_1002"]),
    ("2001",            ACCOUNTS["bank_2001"]),
    ("示例银行B",        ACCOUNTS["bank_2001"]),   # 当前只有 2001 一张示例银行B卡
    ("3001",            ACCOUNTS["bank_3001"]),
    ("示例银行C",        ACCOUNTS["bank_3001"]),   # 当前只有 3001 一张示例银行C卡
    ("示例银行A储蓄卡",  ACCOUNTS["bank_1001"]),   # 默认示例银行A储蓄卡 → 1001
    ("9001",            ACCOUNTS["credit_9001"]),
    ("示例银行A信用卡",  ACCOUNTS["credit_9001"]),
]

# 支付宝专属支付方式
PAYMENT_RULES_ALIPAY = [
    ("亲情卡",          ACCOUNTS["equity_family"]),  # 亲情账户示例：家庭内部结算 → Equity:Internal
    ("花呗",            ACCOUNTS["huabei"]),
    ("余额宝",          ACCOUNTS["alipay_yuebao"]),
    ("账户余额",        ACCOUNTS["alipay_yue"]),
]

# 微信专属支付方式（精确匹配，先匹配"零钱通"再匹配"零钱"）
PAYMENT_RULES_WECHAT = [
    ("零钱通",          ACCOUNTS["wechat_lingqiantong"]),
    ("零钱",            ACCOUNTS["wechat_lingqian"]),
]

# 京东专属支付方式
PAYMENT_RULES_JINGDONG = [
    ("先享后付",        ACCOUNTS["jd_xianxianghoufu"]),
    ("京东小金库",      ACCOUNTS["jd_xiaojinku"]),
    ("京东白条",        ACCOUNTS["jd_baitiao"]),
    ("白条",            ACCOUNTS["jd_baitiao"]),
    # 京东账单里裸「余额」= 京东余额（放最后，避免抢占上面更具体的关键词）。
    # 注：匹配是「关键词 in 文本」，故 2 字的「余额」不会命中支付宝的「余额宝/账户余额」关键词。
    ("余额",            ACCOUNTS["jd_yue"]),
]

# 合并后的完整匹配序列（按来源顺序）
PAYMENT_RULES_ALL = (
    PAYMENT_RULES_COMMON
    + PAYMENT_RULES_ALIPAY
    + PAYMENT_RULES_WECHAT
    + PAYMENT_RULES_JINGDONG
)


# ────────────────────────────────────────────────────────────
# 回溯账期（1902-2301 四个年度）
# ────────────────────────────────────────────────────────────
# 这些老账期里，银行卡（1002 / 示例银行C 3001）的运行余额由银行 PDF 完整提供，
# 而支付宝/微信账单对这些卡的引用并不能与银行流水 1:1 对应（缺失、批量、
# 折算口径不同）。为保证银行卡余额断言成立：
#   - 银行账单全量记在银行卡侧（bean_edit_bank 不做第三方/余额宝跳过）
#   - 支付宝/微信/京东里指向这些卡的支付腿改记 Equity:Transfer（不再扰动银行卡）
# 支出分类仍由支付宝/微信侧保留；银行侧对应腿落 Equity:Unknown（待后续清理）。
BACKFILL_PERIODS = {"1902-2001", "2002-2101", "2102-2201", "2202-2301"}
_BACKFILL_BANK_ACCOUNTS = {ACCOUNTS["bank_1002"], ACCOUNTS["bank_3001"]}


def is_backfill_period(period: str | None = None) -> bool:
    return (period or PERIOD) in BACKFILL_PERIODS


def sanitize_bean_df(df):
    """把 DataFrame 里字符串单元格中的半角双引号替换为单引号，
    避免写入 beancount 字符串字面量（"..."）时破坏语法。"""
    return df.apply(
        lambda col: col.map(lambda x: x.replace('"', "'") if isinstance(x, str) else x)
    )


def get_payment_account(payment_text: str, source: str = "all") -> str:
    """
    根据支付方式文本返回标准账户名。

    source 可选：
        "all"       使用全量规则（默认）
        "alipay"    通用 + 支付宝专属
        "wechat"    通用 + 微信专属
        "jingdong"  通用 + 京东专属
        "bank"      仅通用（银行账单用）
    """
    text = str(payment_text)

    rule_map = {
        "alipay":   PAYMENT_RULES_COMMON + PAYMENT_RULES_ALIPAY,
        "wechat":   PAYMENT_RULES_COMMON + PAYMENT_RULES_WECHAT,
        "jingdong": PAYMENT_RULES_COMMON + PAYMENT_RULES_JINGDONG,
        "bank":     PAYMENT_RULES_COMMON,
        "all":      PAYMENT_RULES_ALL,
    }
    rules = rule_map.get(source, PAYMENT_RULES_ALL)

    for keyword, account in rules:
        if keyword in text:
            # 回溯账期：指向回溯银行卡的支付腿改记内部转账，避免与银行流水重复扰动余额
            if source != "bank" and is_backfill_period() and account in _BACKFILL_BANK_ACCOUNTS:
                return ACCOUNTS["equity_transfer"]
            return account

    return ACCOUNTS["unknown_equity"]


# ────────────────────────────────────────────────────────────
# 支付宝交易分类 → 支出账户（初步分类，reclassifier 再细化）
# ────────────────────────────────────────────────────────────
# 默认账户原则：必须用底层子类（account.bean 已声明），未匹配 reclassifier
# 规则的条目落入对应 :Misc。
ALIPAY_EXPENSE_MAP = {
    "餐饮美食":  "Expenses:Food",
    "日用百货":  "Expenses:Shopping",
    "服饰装扮":  "Expenses:Shopping",
    "交通出行":  "Expenses:Transport",
    "酒店旅游":  "Expenses:Travel",
    "生活服务":  "Expenses:Service",
    "商业服务":  "Expenses:Service",
    "公共服务":  "Expenses:Service",
    "充值缴费":  "Expenses:Service",
    "文化休闲":  "Expenses:Entertainment",
    "美容美发":  "Expenses:Beauty",
    "保险":      "Expenses:Insurance",
    "信用借还":  ACCOUNTS["equity_transfer"],
    "转账红包":  ACCOUNTS["equity_transfer"],
}

# ────────────────────────────────────────────────────────────
# 京东交易分类 → 支出账户
# ────────────────────────────────────────────────────────────
JINGDONG_EXPENSE_MAP_KEYWORDS = [
    ("食品酒",   "Expenses:Food"),
    ("美妆个护", "Expenses:Beauty"),
    ("医疗保健", "Expenses:Health"),
]
JINGDONG_EXPENSE_DEFAULT = "Expenses:Shopping"


def get_jingdong_expense_account(trade_type: str) -> str:
    t = str(trade_type)
    for keyword, account in JINGDONG_EXPENSE_MAP_KEYWORDS:
        if keyword in t:
            return account
    return JINGDONG_EXPENSE_DEFAULT


# ────────────────────────────────────────────────────────────
# fund_map 共用函数（bean_edit_alipay / bean_edit_alipay_funds 共享）
# ────────────────────────────────────────────────────────────
FUND_MAP_PATH = str(_SCRIPT_DIR / "fund_map.json")
ACCOUNT_BEAN_PATH = str(_ROOT_DIR / "bean_files" / "account.bean")


def load_fund_map() -> dict:
    if os.path.exists(FUND_MAP_PATH):
        with open(FUND_MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_fund_map(fm: dict):
    with open(FUND_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(fm, f, ensure_ascii=False, indent=4)


def register_new_fund(fund_name: str, fund_code: str, fund_map: dict, trade_date) -> str:
    commodity_id = f"FUND{fund_code}"
    today        = dt_date.today().strftime("%Y-%m-%d")
    open_date    = (trade_date - timedelta(days=1)).strftime("%Y-%m-%d")
    append_lines = (
        f"\n; 新增基金 - {today}\n"
        f"{open_date} commodity {commodity_id}\n"
        f'  name: "{fund_name}"\n'
        f'{open_date} open Assets:Invest:Fund:{commodity_id} "FIFO"\n'
    )
    with open(ACCOUNT_BEAN_PATH, "a", encoding="utf-8") as f:
        f.write(append_lines)
    fund_map[fund_name] = commodity_id
    save_fund_map(fund_map)
    print(f"[新基金] 已注册: {fund_name} -> {commodity_id}，已追加到 account.bean")
    return commodity_id


# ────────────────────────────────────────────────────────────
# 处理器输出验证
# ────────────────────────────────────────────────────────────
# ────────────────────────────────────────────────────────────
# 账期 ↔ 原始数据目录解析
# ────────────────────────────────────────────────────────────
# 账期编码 "YYMM-YYMM"（如 1902-2001）或单月 "YYMM"（如 2509）。
# 多数来源每个账期一个文件夹；但有的来源把多年合并成一个文件夹
# （如 支付宝基金/1902-2301、示例银行C/1902-2301 覆盖 4 个年度账期）。
# find_raw_dir 先找精确同名目录，找不到再找「范围覆盖本账期」的合并目录。

RAW_DIR = _ROOT_DIR / "原始数据"


def _period_code_bounds(period: str) -> tuple[int, int]:
    """把账期编码解析成 (起, 止) 两个 YYMM 整数，便于做范围包含判断。"""
    p = period.strip()
    if "-" in p:
        a, b = p.split("-", 1)
        return int(a), int(b)
    return int(p), int(p)


def period_date_bounds(period: str) -> tuple[str, str]:
    """年度账期 "YYMM-YYMM" → (起始日, 结束日) ISO 字符串。

    账单年度的实际数据区间为 [起年-01-22, 止年-01-21]（与各来源对账单一致）。
    仅对年度账期有意义；单月账期不会用到此函数（其无合并来源）。
    """
    a, b = period.split("-", 1)
    start_year = 2000 + int(a[:2])
    end_year   = 2000 + int(b[:2])
    return f"{start_year}-01-22", f"{end_year}-01-21"


def find_raw_dir(category: str, period: str) -> Path | None:
    """返回 category 来源下覆盖 period 的原始数据目录。

    1. 精确：原始数据/{category}/{period}
    2. 合并：原始数据/{category}/ 下某个范围覆盖 period 的目录（如 1902-2301）
    找不到返回 None。
    """
    base = RAW_DIR / category
    exact = base / period
    if exact.exists():
        return exact
    if not base.exists():
        return None
    p_start, p_end = _period_code_bounds(period)
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        try:
            c_start, c_end = _period_code_bounds(child.name)
        except ValueError:
            continue
        if c_start <= p_start and c_end >= p_end:
            return child
    return None


# ────────────────────────────────────────────────────────────
# 处理器输出验证
# ────────────────────────────────────────────────────────────
def validate_output(filepath: str, min_rows: int = 1) -> bool:
    """验证处理器输出的 Excel 文件是否存在且有足够行数。
    返回 True 表示通过，False 表示失败（并打印错误信息）。
    """
    p = Path(filepath)
    if not p.exists():
        print(f"❌ 输出文件不存在: {filepath}")
        return False
    try:
        import pandas as pd
        df = pd.read_excel(filepath)
        if len(df) < min_rows:
            print(f"❌ 输出行数不足: {len(df)} 行（期望 ≥ {min_rows}）: {filepath}")
            return False
        print(f"  ✔ 输出验证通过: {len(df)} 行 → {filepath}")
        return True
    except Exception as e:
        print(f"❌ 输出文件读取失败: {e}")
        return False
