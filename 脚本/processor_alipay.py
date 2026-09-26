#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
支付宝 + 支付宝基金 PDF 合并脚本

功能：
    1. 读取支付宝交易流水 CSV
    2. 读取整月支付宝基金交易 PDF
    3. 按 交易订单号 合并确认份额 + 手续费
    4. 输出标准化 Excel

目录结构（相对于本脚本上一级目录）:

原始数据/
├── 支付宝/2601/
├── 支付宝基金/2601/

最终结果/
"""

import os
import re
import pandas as pd
import pdfplumber

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, validate_output, find_raw_dir


# ── 标准列 ─────────────────────────────────────
# 修改后的标准列定义
STANDARD_COLUMNS = [
    '交易时间', '交易类型', '交易对方', '商品',
    '收/支', '金额(元)', '支付方式', '当前状态',
    '交易单号', '商户单号', '备注',
    '确认份额', '手续费', '确认净值', '基金代码', '基金名称', 
    '数据来源'
]
ALIPAY_COL_MAP = {
    '交易时间':     '交易时间',
    '交易分类':     '交易类型',
    '交易对方':     '交易对方',
    '商品说明':     '商品',
    '收/支':        '收/支',
    '金额':         '金额(元)',
    '收/付款方式':  '支付方式',
    '交易状态':     '当前状态',
    '交易订单号':   '交易单号',
    '商家订单号':   '商户单号',
    '备注':         '备注',
}


# ───────────────────────────────────────────────
# 支付宝 CSV 读取
# ───────────────────────────────────────────────

def _find_header_line(file_path: str, encoding: str):
    with open(file_path, 'r', encoding=encoding, errors='replace') as f:
        for i, line in enumerate(f):
            if '交易时间' in line and '交易对方' in line:
                return i
    return -1


def read_alipay_csv(file_path: str) -> pd.DataFrame | None:
    print(f"读取支付宝文件: {os.path.basename(file_path)}")

    header_line = _find_header_line(file_path, 'gbk')
    encoding = 'gbk'
    if header_line == -1:
        header_line = _find_header_line(file_path, 'utf-8')
        encoding = 'utf-8' if header_line != -1 else 'gbk'

    if header_line == -1:
        print("❌ 未找到表头")
        return None

    df = pd.read_csv(
        file_path,
        encoding=encoding,
        skiprows=header_line,
        dtype=str,
        on_bad_lines='skip'
    )

    df.columns = [str(c).strip() for c in df.columns]
    df = df.dropna(axis=1, how='all')

    std = pd.DataFrame()

    for raw_col, std_col in ALIPAY_COL_MAP.items():
        col = raw_col if raw_col in df.columns else None
        if not col:
            for c in df.columns:
                if raw_col in c:
                    col = c
                    break
        std[std_col] = df[col].str.strip() if col else ''

    std['数据来源'] = '支付宝'

    # 过滤合法日期
    std = std[std['交易时间'].str.match(r'\d{4}-\d{2}-\d{2}', na=False)]

    # 金额处理
    std['金额(元)'] = (
        std['金额(元)']
        .str.replace('¥', '', regex=False)
        .str.replace(',', '', regex=False)
        .str.strip()
    )
    std['金额(元)'] = pd.to_numeric(std['金额(元)'], errors='coerce')
    std = std.dropna(subset=['金额(元)'])

    # 新增列
    std['确认份额'] = None
    std['手续费'] = None

    # 保证所有列存在
    for col in STANDARD_COLUMNS:
        if col not in std.columns:
            std[col] = ''

    return std[STANDARD_COLUMNS]


# ───────────────────────────────────────────────
# PDF 解析
# ───────────────────────────────────────────────
def _extract_confirm_date(block: str) -> str:
    """从 PDF 块文本中提取确认日期。

    支付宝基金对账单 PDF 的表格因排版折行，日期被拆成两行：
      行A: "2025/09/1"（YYYY/MM/十位）
      行B: "8 00:00"  （个位 + 确认时间固定 00:00）
    拼合方式: 十位 + 个位 → 完整日期。

    先尝试直接找完整日期（兼容部分 PDF 不折行的情况）。
    """
    from datetime import date as _date

    # ── 尝试 1：完整日期（YYYY/MM/DD 或 YYYY-MM-DD）─────
    full = re.findall(r'\d{4}[-/]\d{2}[-/]\d{2}', block)
    if full:
        return full[-1].replace('/', '-')

    # ── 尝试 2：拼合折行日期 ─────────────────────────────
    # 部分日期：YYYY/MM/D（个位缺失），且 D 后紧跟非数字
    partials = re.findall(r'(\d{4})/(\d{2})/(\d)(?!\d)', block)
    # 确认时间固定为 00:00，前面的单个数字即个位
    ones = re.findall(r'(\d) 00:00', block)

    if not partials or not ones:
        return ""

    year, month, tens = partials[-1]   # 最后一个部分日期 = 确认日期
    unit = ones[-1]                     # 最后一个 00:00 前的个位

    try:
        day = int(tens + unit)
        return _date(int(year), int(month), day).strftime('%Y-%m-%d')
    except ValueError:
        return ""


def parse_fund_pdf_confirm_only(pdf_path):
    full_text = ""
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                full_text += text + "\n"

    # 以交易起始日期作为块分割 (兼容 2019-2029 年份；老对账单为 2019-2023)
    _YEAR_PAT = r"20(?:19|2\d)\d{4}"
    blocks = re.split(rf"(?={_YEAR_PAT})", full_text)
    records = []

    for block in blocks:
        block = block.strip()
        if not re.match(_YEAR_PAT, block):
            continue

        # 1. 提取订单号 (4段8位数字)
        order_parts = re.findall(r"\b\d{8}\b", block)[:4]
        if len(order_parts) < 4:
            continue
        order_id = "".join(order_parts)

        # 2. 提取基金代码 (6位数字)
        code_match = re.search(r"\b\d{6}\b", block)
        fund_code = code_match.group() if code_match else ""

        # 3. 提取交易类型 (确保包含“用户买入”)
        type_match = re.search(r"(定投买入|用户买入|用户卖出)", block)
        trade_type = type_match.group() if type_match else ""

        # 4. 提取金额/份额数字 (使用 \.\d+ 兼容多位小数)
        nums = re.findall(r"\d+\.\d+", block)

        # 5. 【终极改进】乱序基金名称重组逻辑
# --- 基金名称提取：高精度过滤版 ---
        # 1. 匹配：中文、英文、括号、中划线，以及 1-4 位的数字（超过 4 位的数字通常是订单号或日期）
        # 我们用空格把块切开处理
        raw_words = re.findall(r'[\u4e00-\u9fa5A-Za-z()（）\-]+|\b\d{1,4}\b', block)
        
        filtered_parts = []
        for p in raw_words:
            # 排除：已知的交易类型和表头干扰词
            if p in {trade_type, "定投买入", "用户买入", "用户卖出", "组合基金名称", "确认", "成功", "份额", "金额"}:
                continue
            # 排除：纯数字片段如果长度不在 3 位（如 500, 300），且在日期/时间常用数字里，则剔除
            # 这里我们保守一点：只保留和中文字符紧挨着的数字，或者 3 位数的基金标数
            if p.isdigit():
                if len(p) > 4: continue # 踢掉 5 位及以上的数字（绝对不是基金名）
                if p in {"20", "2026", "2025", "01", "02", "12"}: continue # 踢掉年份和月份片段
            
            filtered_parts.append(p)
        
        # 拼接初步名称
        full_name_raw = "".join(filtered_parts)
        
        # 2. 截断逻辑：基金名称一定在交易类型之后，且在基金代码之前
        # 重新利用物理位置进行二次切片
        if trade_type and fund_code:
            # 找到交易类型后的所有字符，直到基金代码出现为止
            name_pattern = fr"{trade_type}(.*?){fund_code}"
            name_match = re.search(name_pattern, block, re.S)
            if name_match:
                content_between = name_match.group(1)
                # 从这段中间内容里，只抓取中文、英文、括号和短数字
                name_pieces = re.findall(r'[\u4e00-\u9fa5A-Za-z()（）\-]+|\b\d{3}\b', content_between)
                fund_name = "".join(name_pieces)
            else:
                fund_name = full_name_raw
        else:
            fund_name = full_name_raw

        # 3. 彻底剔除“串行”：如果名称里出现了其他基金的关键字，截断它
        # 常见的基金公司关键词，如果出现在后面，说明串行了
        for company in ["广发", "天弘", "摩根", "易方达", "嘉实", "博时"]:
            if fund_name.count(company) > 1: # 出现了两次
                fund_name = fund_name.split(company)[0] + company # 只取第一个
            elif company in fund_name and not fund_name.startswith(company):
                # 如果公司名在中间出现，且不是以它开头，检查是否前面已经有一个公司名了
                pass

        # 6. 金额对齐逻辑
        confirm_amount, confirm_share, fee = None, None, None
        if "买入" in trade_type and len(nums) >= 4:
            confirm_amount = float(nums[1])
            confirm_share = float(nums[2])
            fee = float(nums[3])
        elif "卖出" in trade_type and len(nums) >= 5:
            confirm_amount = float(nums[2])
            confirm_share = float(nums[3])
            fee = float(nums[4])

        # 7. 计算净值 (保留 4 位小数)
        confirm_nav = None
        if confirm_amount and confirm_share and confirm_share != 0:
            confirm_nav = round(confirm_amount / confirm_share, 4)

        # 8. 提取确认日期
        # PDF 表格排版折行，日期被拆成两部分：
        #   行A: "2025/09/1"（年月+十位数字）
        #   行B: "8 00:00"（个位数字 + 确认时间固定为 00:00）
        # 先尝试完整格式，找不到再用拼合方式。
        confirm_date = _extract_confirm_date(block)

        # 存入 9 个字段，确保留出基金名称和确认日期的位置
        records.append([
            order_id,
            trade_type,
            fund_code,
            confirm_amount,
            confirm_share,
            fee,
            confirm_nav,
            fund_name,
            confirm_date
        ])

    return records

def read_alipay_fund_pdf(pdf_path: str) -> pd.DataFrame | None:
    print(f"解析基金PDF: {os.path.basename(pdf_path)}")

    records = parse_fund_pdf_confirm_only(pdf_path)

    if not records:
        print("⚠ 未解析到任何基金记录")
        return None

    df = pd.DataFrame(records, columns=[
            "交易单号",
            "交易类型_pdf",
            "基金代码",
            "确认金额",
            "确认份额",
            "手续费",
            "确认净值_pdf",
            "基金名称",
            "确认日期"
        ])

    print(f"PDF解析出 {len(df)} 条基金记录")

    return df
# ───────────────────────────────────────────────
# 主流程
# ───────────────────────────────────────────────
def main():
    ROOT_DIR        = Path(__file__).parent.parent
    ALIPAY_DIR      = find_raw_dir("支付宝", PERIOD)
    ALIPAY_FUND_DIR = find_raw_dir("支付宝基金", PERIOD)
    RESULT_DIR      = ROOT_DIR / "整理后数据" / PERIOD
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    output_path     = RESULT_DIR / f"支付宝整合_{PERIOD}.xlsx"

    if not ALIPAY_DIR or not ALIPAY_DIR.exists():
        print(f"❌ 未找到支付宝目录: 原始数据/支付宝/{PERIOD}")
        sys.exit(1)

    # 1️⃣ 读取支付宝CSV
    csv_files = [
        os.path.join(ALIPAY_DIR, f)
        for f in os.listdir(ALIPAY_DIR)
        if f.lower().endswith(".csv")
    ]

    if not csv_files:
        print("❌ 未找到支付宝CSV文件")
        sys.exit(1)

    alipay_df_list = []
    for file in csv_files:
        df = read_alipay_csv(file)
        if df is not None:
            alipay_df_list.append(df)

    if not alipay_df_list:
        print("❌ 支付宝数据为空")
        sys.exit(1)

    alipay_df = pd.concat(alipay_df_list, ignore_index=True)

    # 统一交易单号格式
    alipay_df["交易单号"] = (
        alipay_df["交易单号"]
        .astype(str)
        .str.strip()
    )

    # 2️⃣ 读取基金PDF
    pdf_files = []
    if ALIPAY_FUND_DIR and ALIPAY_FUND_DIR.exists():
        pdf_files = [
            os.path.join(ALIPAY_FUND_DIR, f)
            for f in os.listdir(ALIPAY_FUND_DIR)
            if f.lower().endswith(".pdf")
        ]

    if not pdf_files:
        print("⚠ 未找到基金PDF，仅输出支付宝数据")
        final_df = alipay_df

    else:
        pdf_path = pdf_files[0]

        pdf_df = read_alipay_fund_pdf(pdf_path)

        if pdf_df is None or pdf_df.empty:
            print("⚠ PDF数据为空，仅输出支付宝数据")
            final_df = alipay_df
        else:

            # 统一交易单号格式
            pdf_df["交易单号"] = pdf_df["交易单号"].astype(str).str.strip()
            alipay_df["交易单号"] = alipay_df["交易单号"].astype(str).str.strip()

            # 做映射字典
            share_map = dict(zip(pdf_df["交易单号"], pdf_df["确认份额"]))
            fee_map   = dict(zip(pdf_df["交易单号"], pdf_df["手续费"]))
            code_map  = dict(zip(pdf_df["交易单号"], pdf_df["基金代码"]))
            name_map  = dict(zip(pdf_df["交易单号"], pdf_df["基金名称"]))
            date_map  = dict(zip(pdf_df["交易单号"], pdf_df["确认日期"]))

            # 执行映射
            alipay_df["确认份额"] = alipay_df["交易单号"].map(share_map)
            alipay_df["手续费"]   = alipay_df["交易单号"].map(fee_map)
            alipay_df["基金代码"] = alipay_df["交易单号"].map(code_map)
            alipay_df["基金名称"] = alipay_df["交易单号"].map(name_map)
            alipay_df["确认日期"] = alipay_df["交易单号"].map(date_map)
        # 3️⃣ 【核心改进】从“商品说明”中提取“基金名称”
            def extract_fund_name(row):
                item = str(row['商品'])
                # 正则逻辑：
                # 1. 尝试匹配 蚂蚁财富-基金名-买入
                # 2. 尝试匹配 基金名-买入
                # 3. 排除掉前缀和后缀
                # (蚂蚁财富-)? 表示可选的前缀
                # (.*?) 是我们要的基金名
                # -(买入|卖出|定投) 是结尾
                pattern = r"^(?:蚂蚁财富-)?(.*?)-(?:买入|卖出|定投|确认销户)"
                match = re.search(pattern, item)
                if match:
                    return match.group(1).strip()
                
                # 如果没匹配到，做简单兜底处理（演示数据使用通用名称）
                if "示例黄金" in item:
                    return "示例黄金基金"
                    
                return ""

            # 只有当该行有“确认份额”（说明是基金交易）时，才去提取名称
            alipay_df["基金名称"] = alipay_df.apply(
                lambda row: extract_fund_name(row) if pd.notna(row["确认份额"]) else "", 
                axis=1
            )
                
            # 3. 计算确认净值
            alipay_df["确认份额"] = pd.to_numeric(
                alipay_df["确认份额"],
                errors="coerce"
            )

            alipay_df["金额(元)"] = pd.to_numeric(
                alipay_df["金额(元)"],
                errors="coerce"
            )

            alipay_df["确认净值"] = alipay_df.apply(
                lambda row: round(row["金额(元)"] / row["确认份额"], 4)
                if pd.notna(row["确认份额"]) and row["确认份额"] != 0
                else None,
                axis=1
            )

            final_df = alipay_df

    # 注：基金买入若「支付方式」为空（支付宝导出对黄金定投等常缺失），不再在此写死
    # 为「示例银行A储蓄卡(1001)」。真实出资账户由 bean_edit_alipay_funds.py 判定：
    # 默认余额宝，若银行流水存在同日同额支付宝扣款则判为银行卡扣款（见该脚本
    # _bank_funded_buy）。早期黄金定投实际从余额宝扣、银行无对应扣款，老规则会
    # 多扣银行卡造成对账差额，故移除。

    # 3️⃣ 拆分基金行与非基金行，分别输出Excel
    # 确认份额不为空的行 = 基金交易行
    if "确认份额" in final_df.columns:
        fund_mask    = final_df["确认份额"].notna()
        fund_df      = final_df[fund_mask].copy()
        non_fund_df  = final_df[~fund_mask].copy()
    else:
        fund_df      = pd.DataFrame(columns=STANDARD_COLUMNS)
        non_fund_df  = final_df.copy()

    # 基金Excel：包含确认日期列
    fund_output_path = RESULT_DIR / f"支付宝基金_{PERIOD}.xlsx"
    fund_columns = STANDARD_COLUMNS + (["确认日期"] if "确认日期" in fund_df.columns else [])
    fund_df[fund_columns].to_excel(fund_output_path, index=False)

    # 整合Excel：仅标准列
    non_fund_df[STANDARD_COLUMNS].to_excel(output_path, index=False)

    print(f"\n✅ 基金Excel: {len(fund_df)}条 → 支付宝基金_{PERIOD}.xlsx")
    print(f"✅ 整合Excel: {len(non_fund_df)}条 → 支付宝整合_{PERIOD}.xlsx")

    if not validate_output(str(output_path)):
        print("❌ 支付宝整合输出验证失败")
        sys.exit(1)
    if len(fund_df) > 0 and not validate_output(str(fund_output_path)):
        print("❌ 支付宝基金输出验证失败")
        sys.exit(1)
    
if __name__ == "__main__":
    main()