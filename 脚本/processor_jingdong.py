#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import pandas as pd
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD, validate_output

STANDARD_COLUMNS = [
    '交易时间', '交易类型', '交易对方', '商品',
    '收/支', '金额(元)', '支付方式', '当前状态',
    '交易单号', '商户单号', '备注', '数据来源'
]


def _find_column(df, target):
    if target in df.columns:
        return target
    for col in df.columns:
        if target in col:
            return col
    return None


def _read_jd_csv(file_path):
    with open(file_path, encoding='utf-8-sig') as f:
        lines = f.readlines()

    header_idx = None
    for i, line in enumerate(lines):
        if '交易时间' in line and '商户名称' in line:
            header_idx = i
            break
    if header_idx is None:
        raise ValueError("未找到表头行")

    headers = [h.strip() for h in lines[header_idx].strip().split(',')]
    rows = []
    for line in lines[header_idx + 1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split('\t')
        time_val       = parts[0].strip().strip(',')
        middle         = parts[1].strip().strip(',') if len(parts) > 1 else ''
        merchant_order = parts[2].strip().strip(',') if len(parts) > 2 else ''
        remark         = parts[3].strip().strip(',') if len(parts) > 3 else ''
        mid_parts = middle.split(',')
        if len(mid_parts) >= 7:
            order_id = mid_parts[-1]
            category = mid_parts[-2]
            inout    = mid_parts[-3]
            status   = mid_parts[-4]
            pay      = mid_parts[-5]
            amount   = mid_parts[-6]
            merchant = mid_parts[0]
            desc     = ','.join(mid_parts[1:-6])
        else:
            merchant = mid_parts[0] if mid_parts else ''
            desc = amount = pay = status = inout = category = order_id = ''
        rows.append([time_val, merchant, desc, amount, pay, status,
                     inout, category, order_id, merchant_order, remark])

    return pd.DataFrame(rows, columns=headers)


def _standardize(df):
    col_map = {
        '交易时间':   '交易时间', '交易分类':   '交易类型', '商户名称':   '交易对方',
        '交易说明':   '商品',     '收/支':      '收/支',    '金额':       '金额(元)',
        '收/付款方式':'支付方式', '交易状态':   '当前状态', '交易订单号': '交易单号',
        '商家订单号': '商户单号', '备注':       '备注',
    }
    std = pd.DataFrame()
    for raw_col, std_col in col_map.items():
        matched = _find_column(df, raw_col)
        std[std_col] = df[matched] if matched else ''
    std['数据来源'] = '京东'

    std['交易时间'] = std['交易时间'].astype(str).str.strip()
    std = std[std['交易时间'].str.match(r'\d{4}-\d{2}-\d{2}', na=False)]

    std['金额(元)'] = (
        std['金额(元)'].astype(str)
        .str.replace('¥', '', regex=False)
        .str.replace(',', '', regex=False)
        .str.replace(r'\(.*?\)', '', regex=True)
        .str.strip()
    )
    std['金额(元)'] = pd.to_numeric(std['金额(元)'], errors='coerce')
    std = std.dropna(subset=['金额(元)']).reset_index(drop=True)

    for col in STANDARD_COLUMNS:
        if col not in std.columns:
            std[col] = ''

    return std[STANDARD_COLUMNS] if not std.empty else None


def read_jd_file(file_path):
    print(f"    读取京东文件: {os.path.basename(file_path)}")
    try:
        if str(file_path).lower().endswith('.csv'):
            df = _read_jd_csv(file_path)
        else:
            df = pd.read_excel(file_path, skiprows=21, dtype=str)
    except Exception as e:
        print(f"    ❌ 读取失败: {e}")
        return None

    if df is None or df.empty:
        print("    ⚠️ 文件读取后为空")
        return None

    df.columns = [str(col).replace('\n', '').replace(' ', '').strip() for col in df.columns]
    print(f"    原始列名: {list(df.columns)}")

    result = _standardize(df)
    if result is not None:
        print(f"    ✅ 成功处理 {len(result)} 条记录")
    return result


def _enrich_wechat_payment(jd_df, wechat_file_path):
    """
    将京东账单中支付方式为"微信支付"的记录，用微信账单里对应的实际支付方式替换。

    两类匹配逻辑：
    - 交易成功（购买）：JD 商户单号 == 微信 商户单号（精确），回退到金额+5分钟时间窗
    - 退款成功（退款）：在微信"收入/已全额退款"行中，金额相同 + 时间差≤5分钟
      （微信退款收入行比JD退款时间晚约3-10秒，商户单号为NaN，无法用单号匹配）
    """
    try:
        wc = pd.read_excel(wechat_file_path, dtype=str)
    except Exception as e:
        print(f"    ⚠️ 读取微信文件失败，跳过支付方式补全: {e}")
        return jd_df

    wc['金额(元)'] = pd.to_numeric(wc['金额(元)'], errors='coerce')
    wc['_dt'] = pd.to_datetime(wc['交易时间'], errors='coerce')

    jd_kw = wc['交易对方'].str.contains('京东|网银在线', na=False) | \
            wc['商品'].str.contains('京东|网银在线', na=False)

    # 购买匹配池：微信支出行（含京东关键词）
    wc_buy = wc[jd_kw & (wc['收/支'] == '支出')].copy()
    # 退款匹配池：微信收入行（含京东关键词，状态含退款）
    wc_ref = wc[jd_kw & (wc['收/支'] == '收入')].copy()

    if wc_buy.empty and wc_ref.empty:
        print("    ⚠️ 微信数据中未找到京东相关记录，跳过补全")
        return jd_df

    jd_df = jd_df.copy()
    jd_df['_dt'] = pd.to_datetime(jd_df['交易时间'], errors='coerce')

    enriched, unmatched = 0, 0

    for idx, row in jd_df.iterrows():
        if str(row.get('支付方式', '')).strip() != '微信支付':
            continue

        amount  = float(row.get('金额(元)', 0))
        status  = str(row.get('当前状态', '')).strip()
        jd_dt   = row['_dt']
        matched = pd.DataFrame()

        if status == '交易成功':
            # 精确：JD 商户单号 == 微信 商户单号
            merchant = str(row.get('商户单号', '')).strip()
            if merchant:
                matched = wc_buy[wc_buy['商户单号'].str.strip() == merchant]
            # 回退：金额 + 时间5分钟内
            if matched.empty:
                tdiff = (wc_buy['_dt'] - jd_dt).abs()
                matched = wc_buy[(wc_buy['金额(元)'] == amount) &
                                 (tdiff <= pd.Timedelta(minutes=5))]

        elif status == '退款成功':
            # 微信退款收入行比JD退款约晚几秒，用金额+时间窗口匹配
            tdiff = (wc_ref['_dt'] - jd_dt).abs()
            matched = wc_ref[(wc_ref['金额(元)'] == amount) &
                             (tdiff <= pd.Timedelta(minutes=5))]

        if matched.empty:
            print(f"    ⚠️ 未匹配: {jd_dt} ¥{amount} ({status})")
            unmatched += 1
            continue

        if len(matched) > 1:
            print(f"    ⚠️ 多条候选（{len(matched)}条），取第一条: {jd_dt} ¥{amount}")

        actual_pay = str(matched.iloc[0]['支付方式']).strip()
        if actual_pay and actual_pay not in ('nan', '/', ''):
            jd_df.at[idx, '支付方式'] = actual_pay
            enriched += 1

    jd_df = jd_df.drop(columns=['_dt'])
    print(f"    ✅ 微信支付方式补全: {enriched} 条成功" +
          (f"，{unmatched} 条未匹配" if unmatched else ""))
    return jd_df


def main():
    ROOT_DIR    = Path(__file__).parent.parent
    JD_DIR      = ROOT_DIR / "原始数据" / "京东" / PERIOD
    RESULT_DIR  = ROOT_DIR / "整理后数据" / PERIOD
    OUTPUT_FILE = RESULT_DIR / f"京东_{PERIOD}.xlsx"

    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"\n📅 当前处理周期: 20{PERIOD[:2]}年{PERIOD[2:]}月")

    if not JD_DIR.exists():
        print("❌ 京东目录不存在:", JD_DIR)
        return

    files = [f for f in os.listdir(JD_DIR) if f.lower().endswith(('.xlsx', '.xls', '.csv'))]
    if not files:
        print("❌ 未找到账单文件")
        return
    if len(files) > 1:
        print("⚠️ 目录中存在多个文件，请确认只有一个")
        return

    df = read_jd_file(JD_DIR / files[0])
    if df is None:
        print("❌ 处理失败")
        return

    # 用微信整合数据补全"微信支付"的实际支付方式
    wechat_file = RESULT_DIR / f"微信_{PERIOD}.xlsx"
    if wechat_file.exists():
        print(f"  🔗 读取微信整合文件补全支付方式...")
        df = _enrich_wechat_payment(df, wechat_file)
    else:
        print(f"  ⚠️ 未找到微信整合文件 ({wechat_file.name})，跳过支付方式补全")
        print(f"     （请先运行 processor_wechat.py）")

    df.to_excel(OUTPUT_FILE, index=False)
    if not validate_output(str(OUTPUT_FILE)):
        print("❌ 京东输出验证失败")
        return
    print(f"\n✅ 输出完成: {OUTPUT_FILE}\n📊 共 {len(df)} 条记录\n")


if __name__ == "__main__":
    main()
