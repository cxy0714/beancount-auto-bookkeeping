#!/usr/bin/env python3
"""
支付宝「账户余额」独立 bean 编辑器
================================================================

把 processor_alipay_yue.py 产出的标准化 Excel 转成一个**独立** .bean 文件，
只补入支付宝 CSV 中缺失（in_csv=False）的余额流水，不触碰任何已生成的账单。

账户路由（主腿恒为 余额 Assets:Cash:Alipay:YuE，对腿按名称/备注分流）：
  - 余额宝赎回 / 购买理财产品转出  → Assets:Cash:Alipay:YuEBao（纯支付宝内部划转）
  - 提现 / 转账 / 转出到余额 / 启动资金 → Equity:Transfer（与银行/他人侧对冲，不扰动已对账余额）
  - 支付-xxx                       → Expenses:Unknown（消费，留给 reclassifier 细化）
  - 收钱码收款 / 收款 / 交易退款      → Income:Unknown（进账，留给 reclassifier 细化）

以上对腿账户均无 balance 断言，故本数据源不会破坏银行/基金对账。

用法（独立运行，不进 pipeline）：
    python 脚本/bean_edit_alipay_yue.py
    python 脚本/bean_edit_alipay_yue.py --dry-run   # 只打印统计，不写文件
"""
import re
import sys
import glob
import argparse
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import ACCOUNTS

ROOT_DIR = Path(__file__).parent.parent
IN_DIR   = ROOT_DIR / "整理后数据" / "支付宝余额"
OUT_DIR  = ROOT_DIR / "bean_files" / "支付宝余额"

YUE    = ACCOUNTS["alipay_yue"]
YUEBAO = ACCOUNTS["alipay_yuebao"]
XFER   = ACCOUNTS["equity_transfer"]
INC_U  = ACCOUNTS["unknown_income"]
EXP_U  = ACCOUNTS["unknown_expense"]
RECONCILIATION_START = pd.Timestamp("2021-09-04")
RECONCILIATION_CUTOFF = pd.Timestamp("2026-08-24 23:59:59")

META_FIELDS = ["流水号", "名称备注", "资金渠道"]


def route_counter_account(note: str, amt: float) -> str:
    """决定对腿账户：内部划转优先，其余按收/支方向落 Income/Expenses:Unknown。"""
    # 1) 余额⇄余额宝内部划转：余额 PDF 只负责补余额这一侧。
    #
    # 余额宝本身同时有独立流水 PDF，且支付宝交易 CSV 通常已经包含
    # 余额宝那一侧。若这里再把对腿记到 YuEBao，会把同一笔内部转账
    # 计入两次（典型表现是余额宝历史余额成倍增长）。对腿改挂中性
    # Transfer；余额宝一侧由自己的流水/基金编辑器负责。
    if "余额宝" in note or "购买理财产品转出" in note:
        return XFER
    # 2) 自有账户/他人之间的转账类（提现、转账、转出到余额、启动资金存放）→ 中性转账平账
    if "提现" in note or note.startswith("转账") or "转出到余额" in note or "启动资金" in note:
        return XFER
    # 3) 其余（支付/收款/退款等）按方向：进账=收入，出账=支出，待 reclassifier 细化
    return INC_U if amt > 0 else EXP_U


def _payee_goods(note: str) -> tuple[str, str]:
    """把「名称/备注」拆成 (payee, goods)。形如 "支付-收钱码收款" → ("支付","收钱码收款")。"""
    if "/" in note:
        a, b = note.split("/", 1)
        return a, b
    if "-" in note:
        a, b = note.split("-", 1)
        return a, b
    return note, note


def build_entries(df: pd.DataFrame) -> tuple[list[str], dict]:
    stats = {"total": len(df), "by_acct": {}}
    lines = []
    for _, row in df.iterrows():
        dt = pd.to_datetime(row["交易时间"])
        amt = float(row["金额(元)"])          # 收入>0 / 支出<0：即 余额 的变动符号
        note = str(row["名称备注"])
        counter = route_counter_account(note, amt)
        stats["by_acct"][counter] = stats["by_acct"].get(counter, 0) + 1
        payee, goods = _payee_goods(note)

        meta = []
        meta.append(f'  time: "{dt.strftime("%Y-%m-%d %H:%M:%S")}"')
        meta.append(f'  serial: "{row["流水号"]}"')
        meta.append(f'  source: "支付宝余额"')

        head = f'{dt.strftime("%Y-%m-%d")} * "{payee}" "{goods}"'
        # 主腿：余额账户按 amt 变动；对腿取相反数自动配平
        leg_yue   = f'  {YUE:<55} {amt:>10.2f} CNY'
        leg_other = f'  {counter:<55} {-amt:>10.2f} CNY'
        lines.append("\n".join([head, *meta, leg_yue, leg_other]) + "\n")
    return lines, stats


def make_header(rng: str, stats: dict, n_total_pdf: int, n_in_csv: int) -> str:
    acct_lines = "\n".join(
        f";     - {acct:<28} {cnt}" for acct, cnt in sorted(stats["by_acct"].items())
    )
    return (
        "; ==================================================\n"
        f"; 支付宝账户余额 {rng} 自动生成（独立数据源 / 不进 pipeline）\n"
        f"; 来源 PDF 明细总数:        {n_total_pdf}\n"
        f"; 已在支付宝 CSV 中(跳过):  {n_in_csv}\n"
        f"; 本文件补入(CSV 缺失):     {stats['total']}\n"
        "; --------------------------------------------------\n"
        "; 对腿账户分布:\n"
        f"{acct_lines}\n"
        "; ==================================================\n\n"
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只打印统计，不写文件")
    args = ap.parse_args()

    xlsxs = sorted(glob.glob(str(IN_DIR / "支付宝余额_*.xlsx")))
    if not xlsxs:
        print(f"❌ 未找到输入 Excel：{IN_DIR}/支付宝余额_*.xlsx（先跑 processor_alipay_yue.py）")
        sys.exit(1)

    emitted_missing = set()
    for xlsx in xlsxs:
        rng = re.search(r"支付宝余额_(.+)\.xlsx", Path(xlsx).name).group(1)
        df = pd.read_excel(xlsx)
        n_total_pdf = len(df)
        n_in_csv = int(df["in_csv"].sum())
        df["交易时间"] = pd.to_datetime(df["交易时间"], errors="coerce")
        df = df[
            (df["交易时间"] >= RECONCILIATION_START)
            & (df["交易时间"] <= RECONCILIATION_CUTOFF)
        ].copy()
        miss = df[~df["in_csv"]].copy().sort_values("交易时间").reset_index(drop=True)
        # 多份 PDF 的覆盖区间可能重叠（例如 2102-2605 与 2606）；
        # 用完整流水字段跨文件去重，保证重新生成不重复。
        keys = ["交易时间", "金额(元)", "名称备注", "账户余额"]
        keep = []
        for i, row in miss.iterrows():
            key = tuple(row[k] for k in keys)
            if key not in emitted_missing:
                emitted_missing.add(key)
                keep.append(i)
        miss = miss.loc[keep].reset_index(drop=True)

        lines, stats = build_entries(miss)
        header = make_header(rng, stats, n_total_pdf, n_in_csv)

        print(f"\n范围 {rng}：PDF {n_total_pdf} 条，CSV 已含 {n_in_csv}，补入 {stats['total']} 条")
        for acct, cnt in sorted(stats["by_acct"].items()):
            print(f"  对腿 {acct:<30} {cnt}")

        if args.dry_run:
            print("  [dry-run] 未写文件")
            continue

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        out_path = OUT_DIR / f"alipay_yue_{rng}.bean"
        out_path.write_text(header + "\n".join(lines), encoding="utf-8")
        print(f"  ✅ 输出：{out_path}")
        print(f"  → 请在 main.bean 中 include \"支付宝余额/alipay_yue_{rng}.bean\"")


if __name__ == "__main__":
    main()
