#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成一套完全虚拟的演示原始数据。

用途：公开仓库中只包含虚拟数据，不包含任何真实账单。
生成后的目录结构与真实导出格式保持一致，便于演示 pipeline。
"""
from pathlib import Path
import csv
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "原始数据"

# 虚拟卡号（与 脚本/config.py 中 BANK_CARDS 对应）
CARD_A_DEBIT = "6217000000001001"
CARD_B_DEBIT = "6217000000002001"
CARD_C_DEBIT = "6217000000003001"
CARD_A_CREDIT = "6227000000009001"


def _write_csv(path: Path, rows, encoding="utf-8-sig"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding=encoding, newline="") as f:
        w = csv.writer(f)
        w.writerows(rows)


def _font_prop():
    import matplotlib.font_manager as fm
    for p in [r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\simhei.ttf"]:
        if Path(p).exists():
            return fm.FontProperties(fname=p)
    return None


def write_text_pdf(path: Path, lines: list[str]):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    path.parent.mkdir(parents=True, exist_ok=True)
    font = _font_prop()
    fig = plt.figure(figsize=(12, 8))
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    y = 0.96
    for line in lines:
        ax.text(0.03, y, line, fontproperties=font, fontsize=10, va="top")
        y -= 0.038
    with PdfPages(path) as pdf:
        pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def write_table_pdf(path: Path, title_lines: list[str], columns: list[str], rows: list[list[str]],
                    figsize=(24, 6), fontsize=7):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    path.parent.mkdir(parents=True, exist_ok=True)
    font = _font_prop()
    fig = plt.figure(figsize=figsize)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    y = 0.97
    for line in title_lines:
        ax.text(0.01, y, line, fontproperties=font, fontsize=9, va="top")
        y -= 0.04

    table_ax = fig.add_axes([0.01, 0.08, 0.98, max(0.15, y - 0.12)])
    table_ax.axis("off")
    table = table_ax.table(cellText=rows, colLabels=columns, loc="center", cellLoc="center")
    table.auto_set_font_size(False)
    table.set_fontsize(fontsize)
    try:
        table.auto_set_column_width(col=list(range(len(columns))))
    except Exception:
        pass
    for _, cell in table.get_celld().items():
        cell.set_text_props(fontproperties=font)
        cell.set_edgecolor("black")
        cell.set_linewidth(0.4)
    with PdfPages(path) as pdf:
        pdf.savefig(fig, bbox_inches="tight")
    plt.close(fig)


def gen_alipay():
    rows = [
        ["支付宝交易明细（虚拟）"],
        ["导出时间：2025-01-22 00:00:00"],
        ["共 5 笔记录"],
        ["----------------------交易记录--------------------"],
        ["交易时间", "交易分类", "交易对方", "商品说明", "收/支", "金额", "收/付款方式", "交易状态", "交易订单号", "商家订单号", "备注"],
        ["2025-01-05 12:00:00", "餐饮美食", "示例咖啡", "拿铁中杯", "支出", "25.00", "余额宝", "交易成功", "AP001", "APM001", ""],
        ["2025-01-06 18:30:00", "日用百货", "示例超市", "日用品", "支出", "88.00", "余额宝", "交易成功", "AP002", "APM002", ""],
        ["2025-01-10 08:00:00", "交通出行", "示例地铁", "地铁票", "支出", "4.00", "账户余额", "交易成功", "AP003", "APM003", ""],
        ["2025-01-15 09:00:00", "转账红包", "示例用户", "朋友转账", "收入", "100.00", "账户余额", "交易成功", "AP004", "APM004", ""],
        ["2025-01-08 10:00:00", "投资理财", "示例基金公司", "示例稳健基金A-买入", "支出", "1000.00", "示例银行A储蓄卡(1001)", "交易成功", "20250108111111112222222233333333", "APM005", "虚拟基金申购"],
    ]
    _write_csv(RAW / "支付宝" / "2501" / "支付宝交易明细(20241222-20250121).csv", rows)


def gen_alipay_fund_pdf():
    path = RAW / "支付宝基金" / "2501" / "基金交易明细_示例.pdf"
    lines = [
        "支付宝基金交易确认单（虚拟）",
        "20250108 用户买入 示例稳健基金A 999999 11111111 22222222 33333333",
        "确认日期 2025-01-08",
        "确认净值 1.0000 确认金额 1000.00 确认份额 1000.0000 手续费 1.00",
        "备注：本文件为虚拟演示数据，不对应任何真实交易。",
    ]
    write_text_pdf(path, lines)


def gen_wechat():
    path = RAW / "微信" / "2501" / "微信支付账单流水文件(示例)_20250122.xlsx"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [[None] * 6 for _ in range(18)]
    rows[0][0] = "微信支付账单明细（虚拟）"
    rows[1][0] = "微信昵称：[示例用户]"
    rows[2][0] = "起始时间：[2024-12-22 00:00:00] 终止时间：[2025-01-21 23:59:59]"
    rows[3][0] = "导出类型：[全部账单]"
    rows[4][0] = "导出时间：[2025-01-22 00:00:00]"
    rows[6][0] = "共 3 笔记录"
    rows[7][0] = "收入：1 笔 50.00 元"
    rows[8][0] = "支出：2 笔 113.00 元"
    rows[16][0] = "----------------------微信支付账单明细列表--------------------"
    header = ["交易时间", "交易类型", "交易对方", "商品", "收/支", "金额(元)", "支付方式", "当前状态", "交易单号", "商户单号", "备注"]
    rows[17] = header + [None] * (6 - len(header)) if len(header) > 6 else header
    # 重新构造 11 列，多余列留空
    fixed = []
    for r in rows:
        fixed.append((r + [None] * 11)[:11])
    data = [
        ["2025-01-07 19:00:00", "商户消费", "示例书店", "技术图书", "支出", "40.00", "零钱", "支付成功", "WX001", "WXM001", ""],
        ["2025-01-11 12:30:00", "商户消费", "示例餐厅", "午餐", "支出", "30.00", "零钱", "支付成功", "WX002", "WXM002", ""],
        ["2025-01-15 09:00:00", "转账", "示例用户", "红包", "收入", "50.00", "零钱", "支付成功", "WX003", "WXM003", ""],
    ]
    fixed.extend(data)
    pd.DataFrame(fixed).to_excel(path, index=False, header=False)


def gen_jingdong():
    path = RAW / "京东" / "2501" / "京东交易流水(示例)_001.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines += ["导出信息：", "京东账号名：demo_user", "申请时间：2025-01-22 00:00:00", "日期区间：2025-01-01 至 2025-01-21", "导出交易类型：全部", "导出交易场景：全部", "共：2 笔记录", "收入：0 笔，0.00 元", "支出：2 笔，86.00 元", "", "特别提示", ""]
    lines.append("交易时间,商户名称,交易说明,金额,收/付款方式,交易状态,收/支,交易分类,交易订单号,商家订单号,备注")
    lines.append("2025-01-07 14:00:00\t,示例京东商户,示例商品,66.00,示例银行A信用卡(9001),交易成功,支出,食品酒饮,JD001\t,JDM001\t, ,")
    lines.append("2025-01-09 11:30:00\t,示例京东商户,日用品,20.00,示例银行A信用卡(9001),交易成功,支出,日用百货,JD002\t,JDM002\t, ,")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        f.write("\n".join(lines) + "\n")


def gen_boc_pdf():
    path = RAW / "示例银行A" / "2501" / "交易流水明细_示例.pdf"
    cols = ["序号", "记账日期", "记账时间", "交易名称", "对方账户名", "附言", "金额", "余额", "币别", "渠道", "对方卡号/账号", "对方开户行", "网点名称", "交易场所"]
    rows = [
        ["1", "2025-01-03", "09:00:00", "短信服务费", "示例银行A", "短信服务费", "-3.00", "997.00", "人民币", "系统扣款", "", "示例支行", "示例网点", ""],
        ["2", "2025-01-08", "10:00:00", "消费", "支付宝", "示例稳健基金A-买入", "-1000.00", "-25.00", "人民币", "快捷支付", "", "示例支行", "示例网点", ""],
        ["3", "2025-01-15", "09:00:00", "补贴", "示例机构", "补贴收入", "5000.00", "4975.00", "人民币", "手机银行", "", "示例支行", "示例网点", ""],
    ]
    write_table_pdf(path, [f"借记卡号：{CARD_A_DEBIT}", "示例银行A交易流水明细（虚拟）"], cols, rows, figsize=(34, 7), fontsize=6.5)


def gen_cmb_pdf():
    path = RAW / "示例银行B" / "2501" / "示例银行B交易流水_示例.pdf"
    lines = [
        "示例银行B交易流水（虚拟）",
        f"账号：{CARD_B_DEBIT}",
        "记账日期 币种 交易金额 联机余额 交易摘要 对手信息",
        "2025-01-09 CNY -120.00 880.00 消费 示例超市 6227000000001001",
        "2025-01-11 CNY 200.00 1080.00 转账 示例用户 6217000000002001",
    ]
    write_text_pdf(path, lines)


def gen_ccb_pdf():
    path = RAW / "示例银行C" / "2501" / "示例银行C交易流水_示例.pdf"
    cols = ["序号", "摘要", "交易日期", "交易金额", "账户余额", "交易地点/附言", "对方账号与户名"]
    rows = [
        ["1", "消费", "20250112", "-30.00", "970.00", "示例餐厅", "6227000000004001/示例商户"],
        ["2", "转账", "20250115", "500.00", "1470.00", "示例机构", "6217000000001001/示例用户"],
    ]
    write_table_pdf(path, [f"卡号/账号:{CARD_C_DEBIT}", "示例银行C个人活期账户交易明细（虚拟）"], cols, rows, figsize=(18, 5), fontsize=8)


def gen_creditcard_pdf():
    path = RAW / "示例银行A信用卡" / "2501" / "示例银行A信用卡电子合并账单2025年01月账单.PDF"
    cols = ["交易日期", "记账日期", "卡号后4位", "交易描述", "存入", "支出"]
    rows = [
        ["01/07", "01/07", "9001", "网银在线-示例京东商户", "", "66.00"],
        ["01/16", "01/16", "9001", "示例影音会员-年度订阅", "", "15.00"],
        ["01/20", "01/20", "9001", "还款成功", "100.00", ""],
    ]
    write_table_pdf(path, ["示例银行A信用卡电子合并账单（虚拟）", "账单月份：2025年01月"], cols, rows, figsize=(16, 5), fontsize=8)


def gen_alipay_yue_pdf():
    path = RAW / "支付宝余额" / "2501" / "收支明细证明_示例.pdf"
    cols = ["流水号", "时间", "名称/备注", "收入", "支出", "账户余额", "资金渠道"]
    rows = [
        ["Y001", "2025-01-15 09:00:00", "朋友转账", "100.00", "", "100.00", "账户余额"],
        ["Y002", "2025-01-10 08:00:00", "地铁票", "", "4.00", "0.00", "账户余额"],
    ]
    write_table_pdf(path, ["支付宝余额收支明细证明（虚拟）"], cols, rows, figsize=(18, 5), fontsize=8)


def main():
    gen_alipay()
    gen_alipay_fund_pdf()
    gen_wechat()
    gen_jingdong()
    gen_boc_pdf()
    gen_cmb_pdf()
    gen_ccb_pdf()
    gen_creditcard_pdf()
    gen_alipay_yue_pdf()
    print("演示原始数据已生成：", RAW)


if __name__ == "__main__":
    main()
