#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
支付宝基金交易 → Beancount 转换脚本

读取: 整理后数据/{PERIOD}/支付宝基金_{PERIOD}.xlsx
输出: bean_files/{PERIOD}/alipay_funds_{PERIOD}.bean

每笔基金交易生成两条分录：
  买入: T+0 (扣款日) + T+N (份额确认日)
  卖出: T+0 (份额确认/赎回日) + T+N (到账日)
"""

import pandas as pd
import os
import re
import glob
import collections
from datetime import date as dt_date
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, ACCOUNTS, get_payment_account, load_fund_map, save_fund_map, register_new_fund, sanitize_bean_df

# ──────────────────────────────────────────
# 路径
# ──────────────────────────────────────────
SCRIPT_DIR    = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR      = str(Path(SCRIPT_DIR).parent)

input_path    = os.path.join(ROOT_DIR, f"整理后数据/{PERIOD}/支付宝基金_{PERIOD}.xlsx")
output_path   = os.path.join(ROOT_DIR, f"bean_files/{PERIOD}/alipay_funds_{PERIOD}.bean")
account_path  = os.path.join(ROOT_DIR, "bean_files/account.bean")

# 元数据键名映射（与 bean_edit_alipay.py 一致）
META_MAP = {
    '交易时间': 'time',  '交易类型': 'type',   '交易对方': 'payee_raw',
    '商品':     'goods', '收/支':    'flow',    '当前状态': 'status',
    '备注':     'remark','数据来源': 'source',
}


# ──────────────────────────────────────────
# 工具函数
# ──────────────────────────────────────────
def _pay(text):
    return get_payment_account(str(text), source="alipay")


# 银行流水缓存：(日期, |金额|) → 当日是否存在同额「支付宝」扣款
_bank_debits_cache = None
_cash_debits_cache = None
RECONCILIATION_START = dt_date(2021, 9, 4)
RECONCILIATION_CUTOFF = dt_date(2026, 8, 24)


def _pdf_amount(cell):
    try:
        value = float(str(cell).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return value if abs(value) > 1e-9 else None


def _load_statement_debits():
    """加载截止 2026-08-24 的余额/余额宝支出，用于基金买入配对。"""
    global _cash_debits_cache
    if _cash_debits_cache is not None:
        return _cash_debits_cache

    cutoff = RECONCILIATION_CUTOFF
    statements = {"yue": collections.Counter(), "yuebao": collections.Counter()}
    try:
        import pdfplumber
    except ImportError:
        _cash_debits_cache = statements
        return statements

    def read_files(pattern, account):
        for filename in glob.glob(os.path.join(ROOT_DIR, pattern)):
            try:
                pdf = pdfplumber.open(filename)
            except Exception:
                continue
            with pdf:
                for page in pdf.pages:
                    for table in page.extract_tables():
                        for row in table:
                            if not row or len(row) < 4 or not row[0]:
                                continue
                            if account == "yuebao":
                                time_cell, amount = row[0], _pdf_amount(row[1])
                            else:
                                if len(row) < 7:
                                    continue
                                time_cell = row[1]
                                expense = _pdf_amount(row[4])
                                income = _pdf_amount(row[3])
                                amount = -abs(expense) if expense is not None else income
                            if amount is None:
                                continue
                            text = re.sub(r"\s+", "", str(time_cell))
                            match = re.match(
                                r"(20\d{2}-\d{2}-\d{2})(\d{2}:\d{2}(?::\d{2})?)?",
                                text,
                            )
                            if not match:
                                continue
                            try:
                                trade_date = dt_date.fromisoformat(match.group(1))
                            except ValueError:
                                continue
                            if trade_date <= cutoff and amount < 0:
                                statements[account][
                                    (trade_date, round(abs(float(amount)), 2))
                                ] += 1

    read_files("原始数据/支付宝余额/*/*.pdf", "yue")
    read_files("原始数据/支付宝余额宝/*.pdf", "yuebao")
    _cash_debits_cache = statements
    return statements


def _load_bank_alipay_debits():
    """汇总本账期所有银行卡 Excel 中「交易对方含支付宝、收/支为支出」的 (日期, |金额|)。

    bean_edit_bank 会把银行流水里的支付宝快捷支付当作第三方重复流水跳过，因此基金
    买入的扣款腿是这笔支出的唯一记账来源——可借此判定该笔买入是否真从银行卡扣款。
    """
    global _bank_debits_cache
    if _bank_debits_cache is not None:
        return _bank_debits_cache
    debits = set()
    bank_glob = os.path.join(ROOT_DIR, f"整理后数据/{PERIOD}")
    if os.path.isdir(bank_glob):
        for fn in os.listdir(bank_glob):
            if not (fn.startswith("银行卡") and fn.endswith(".xlsx")):
                continue
            try:
                bdf = pd.read_excel(os.path.join(bank_glob, fn))
            except Exception:
                continue
            cols = bdf.columns
            tcol = next((c for c in cols if "交易时间" in c), None)
            acol = next((c for c in cols if c == "金额(元)" or "金额" in c), None)
            pcol = next((c for c in cols if "收/支" in c), None)
            ccol = next((c for c in cols if "交易对方" in c), None)
            if not (tcol and acol):
                continue
            t = pd.to_datetime(bdf[tcol], errors="coerce")
            for i in range(len(bdf)):
                if pd.isna(t.iloc[i]):
                    continue
                if pcol is not None and str(bdf[pcol].iloc[i]) != "支出":
                    continue
                if ccol is not None and "支付宝" not in str(bdf[ccol].iloc[i]):
                    continue
                try:
                    amt = round(abs(float(bdf[acol].iloc[i])), 2)
                except (TypeError, ValueError):
                    continue
                debits.add((t.iloc[i].date(), amt))
    _bank_debits_cache = debits
    return debits


def _bank_funded_buy(trade_date, amount) -> bool:
    """该笔基金买入当日银行卡是否有同额支付宝扣款（→ 真从银行卡出资）。"""
    return (trade_date, round(abs(float(amount)), 2)) in _load_bank_alipay_debits()


def _infer_cash_funded_buy(trade_date, amount):
    """银行卡未匹配时，用余额/余额宝流水一对一确定资金来源。"""
    key = (trade_date, round(abs(float(amount)), 2))
    statements = _load_statement_debits()
    yue_hit = statements["yue"][key] > 0
    yuebao_hit = statements["yuebao"][key] > 0

    if yue_hit and not yuebao_hit:
        statements["yue"][key] -= 1
        return ACCOUNTS["alipay_yue"]
    if yuebao_hit and not yue_hit:
        statements["yuebao"][key] -= 1
        return ACCOUNTS["alipay_yuebao"]

    if yue_hit and yuebao_hit:
        print(f"[警告] 基金买入来源冲突（日/金额同时出现在余额和余额宝）: {trade_date} {amount:.2f}，暂用余额宝")
    else:
        print(f"[警告] 基金买入未找到余额/余额宝扣款: {trade_date} {amount:.2f}，暂用余额宝")
    return ACCOUNTS["alipay_yuebao"]


def get_meta_lines(row):
    lines = []
    for cn, en in META_MAP.items():
        val = str(row.get(cn, ""))
        val = val.replace('\r', ' ').replace('\n', ' ')
        val = ' '.join(val.split()).strip()
        if val and val not in ["nan", "/", ""]:
            lines.append(f'  {en}: "{val}"')
    return lines


def get_fund_commodity(fund_name, row, fund_map, trade_date):
    if fund_name in fund_map:
        return fund_map[fund_name]
    raw_code = str(row.get("基金代码", "")).strip()
    if raw_code and raw_code not in ("nan", ""):
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
df_raw   = sanitize_bean_df(pd.read_excel(input_path))

# 保留有份额的行（双重保险）
if "确认份额" in df_raw.columns:
    df_raw = df_raw[df_raw["确认份额"].notna()].copy()

df_raw = df_raw.fillna("")

# 解析日期
df_raw["交易时间"] = pd.to_datetime(df_raw["交易时间"])
df_raw = df_raw.sort_values(by=["交易时间", "交易单号"]).reset_index(drop=True)

bean_lines   = []
entry_count  = 0

for _, row in df_raw.iterrows():
    product    = str(row.get("商品", ""))
    payee      = str(row.get("交易对方", ""))
    trade_date = row["交易时间"].date()
    amount     = float(row.get("金额(元)", 0) or 0)
    payment    = str(row.get("支付方式", ""))
    meta       = get_meta_lines(row)

    # 手续费（NaN → 0）
    fee_raw = row.get("手续费", "")
    try:
        fee = float(fee_raw) if str(fee_raw) not in ("", "nan") else 0.0
    except (ValueError, TypeError):
        fee = 0.0

    # 份额（已过滤，但防御性处理）
    shares_raw = row.get("确认份额", "")
    try:
        shares = float(shares_raw)
    except (ValueError, TypeError):
        print(f"[跳过] 无法解析份额: {shares_raw}，行: {product}")
        continue

    # 确认日期（fallback 到 trade_date）
    cd_raw = str(row.get("确认日期", "")).strip()
    if cd_raw and cd_raw not in ("nan", ""):
        try:
            confirm_date = dt_date.fromisoformat(cd_raw)
        except ValueError:
            confirm_date = trade_date
    else:
        confirm_date = trade_date

    # 基金名称 & 商品代码
    fund_name = str(row.get("基金名称", "")).strip()
    if not fund_name or fund_name == "nan":
        fund_name = product  # 兜底

    commodity_id = get_fund_commodity(fund_name, row, fund_map, trade_date)
    if not commodity_id:
        print(f"[跳过] 无法获取基金商品ID，跳过: {product}")
        continue

    fund_account = f"Assets:Invest:Fund:{commodity_id}"

    # ──────────────────────────────────────────
    # 买入 (买入 / 定投)
    # ──────────────────────────────────────────
    if "买入" in product or "定投" in product:
        net_invested = round(amount - fee, 2)
        actual_nav   = round(net_invested / shares, 4) if shares != 0 else 0.0
        payment_acc  = _pay(payment)
        if payment_acc == ACCOUNTS["unknown_equity"]:
            if RECONCILIATION_START <= trade_date <= RECONCILIATION_CUTOFF:
                # 历史补账窗口：只在支付宝余额与余额宝之间判断。
                # 银行卡流水虽完整，但基金交易的支付宝支付腿不能因此改挂银行卡；
                # 银行账本是已核对基线，不在本次余额/余额宝补账中改写。
                payment_acc = _infer_cash_funded_buy(trade_date, amount)
            else:
                # 新账期：银行卡流水完整，优先按同日同额支付宝扣款判定银行卡出资。
                payment_acc = (ACCOUNTS["bank_1001"]
                               if _bank_funded_buy(trade_date, amount)
                               else ACCOUNTS["alipay_yuebao"])

        # T+0: 扣款日，资金从支付账户流入 Pending
        bean_lines.append(f'{trade_date} * "{payee}" "{product}"')
        bean_lines.extend(meta)
        pending_line = f'  {ACCOUNTS["fund_pending"]:<55} {net_invested:>10.2f} CNY'
        if fee > 0:
            fee_line     = f'  {ACCOUNTS["invest_fee"]:<55} {fee:>10.2f} CNY'
            payment_line = f'  {payment_acc:<55} {-amount:>10.2f} CNY'
            bean_lines.append(f'{pending_line}\n{fee_line}\n{payment_line}\n')
        else:
            payment_line = f'  {payment_acc:<55} {-amount:>10.2f} CNY'
            bean_lines.append(f'{pending_line}\n{payment_line}\n')

        # T+N: 份额确认日，Pending → 基金持仓
        confirm_date_str = confirm_date.strftime("%Y-%m-%d")
        bean_lines.append(f'{confirm_date} * "{payee}" "{product}-确认"')
        bean_lines.append(f'  confirm_date: "{confirm_date_str}"')
        fund_line    = f'  {fund_account:<55} {shares:>15.4f} {commodity_id} {{{actual_nav:.4f} CNY}}'
        pending_out  = f'  {ACCOUNTS["fund_pending"]:<55} {-net_invested:>10.2f} CNY'
        rounding_line = f'  {ACCOUNTS["equity_rounding"]}'
        bean_lines.append(f'{fund_line}\n{pending_out}\n{rounding_line}\n')

        entry_count += 1

    # ──────────────────────────────────────────
    # 卖出 (赎回)
    # ──────────────────────────────────────────
    elif "卖出" in product or "赎回" in product or "确认销户" in product:
        gross_proceeds = round(amount + fee, 2)
        actual_nav     = round(gross_proceeds / shares, 4) if shares != 0 else 0.0
        # "至银行卡"/"到银行卡"：资金直接入银行卡，payment 列写的"余额宝"是关联账户而非真实去向
        if "至银行卡" in product or "到银行卡" in product:
            cash_acc = ACCOUNTS["bank_1001"]
        else:
            cash_acc = _pay(payment)
            if cash_acc == ACCOUNTS["unknown_equity"]:
                cash_acc = ACCOUNTS["alipay_yuebao"]  # 默认回笼到余额宝

        # T+0: 份额确认日，份额离开持仓 → SalePending
        confirm_date_str = confirm_date.strftime("%Y-%m-%d")
        bean_lines.append(f'{confirm_date} * "{payee}" "{product}-份额确认"')
        bean_lines.extend(meta)
        sale_pending_line = f'  {ACCOUNTS["fund_sale_pending"]:<55} {gross_proceeds:>10.2f} CNY'
        fund_out_line     = f'  {fund_account:<55} {-shares:>15.4f} {commodity_id} {{}} @ {actual_nav:.4f} CNY'
        gains_line        = f'  {ACCOUNTS["income_invest"]}'
        bean_lines.append(f'{sale_pending_line}\n{fund_out_line}\n{gains_line}\n')

        # T+N: 到账日，SalePending → 收款账户
        bean_lines.append(f'{trade_date} * "{payee}" "{product}-到账"')
        bean_lines.append(f'  confirm_date: "{confirm_date_str}"')
        cash_line         = f'  {cash_acc:<55} {amount:>10.2f} CNY'
        sale_pending_out  = f'  {ACCOUNTS["fund_sale_pending"]:<55} {-gross_proceeds:>10.2f} CNY'
        if fee > 0:
            fee_line = f'  {ACCOUNTS["invest_fee"]:<55} {fee:>10.2f} CNY'
            bean_lines.append(f'{cash_line}\n{fee_line}\n{sale_pending_out}\n')
        else:
            bean_lines.append(f'{cash_line}\n{sale_pending_out}\n')

        entry_count += 1

    else:
        print(f"[警告] 无法识别买卖方向，跳过: {product}")
        continue

# ──────────────────────────────────────────
# 写文件
# ──────────────────────────────────────────
os.makedirs(os.path.join(ROOT_DIR, f"bean_files/{PERIOD}"), exist_ok=True)

# 2102-2201 文件开头包含手工核验的早期定投确认腿回溯补录。
# 重跑编辑器时必须保留该区块，否则会把确认份额删掉，造成历史基金 lot 不足。
preserved_prefix = ""
if os.path.isfile(output_path):
    with open(output_path, encoding="utf-8") as existing_file:
        existing_text = existing_file.read()
    marker = "; === 早期定投确认腿回溯补录"
    marker_pos = existing_text.find(marker)
    first_generated = existing_text.find("\n2021-05-31 *", marker_pos)
    if marker_pos >= 0 and first_generated > marker_pos:
        preserved_prefix = existing_text[marker_pos:first_generated]

header = (
    f"; ==================================================\n"
    f"; 支付宝基金 {PERIOD} 自动生成\n"
    f"; 共处理基金交易: {entry_count} 笔（每笔生成两条分录）\n"
    f"; ==================================================\n\n"
)

with open(output_path, "w", encoding="utf-8") as f:
    f.write(header)
    if preserved_prefix:
        f.write(preserved_prefix)
    f.writelines("\n".join(bean_lines))

print(f"✅ 基金Bean生成完成: {entry_count} 笔 → alipay_funds_{PERIOD}.bean")
