#!/usr/bin/env python3
"""
支付宝「账户余额」收支明细证明 PDF 处理器（独立数据源）
================================================================

背景：支付宝交易明细 CSV 不含「账户余额」(Assets:Cash:Alipay:Balance) 的内部流水
（余额宝赎回转入/转出、提现、收钱码收款、转账等）。支付宝可单独导出一份
《收支明细证明》PDF（一个文件覆盖多年，如 2102-2605），本脚本把它解析成标准化
Excel，并对每条记录标注「是否已在支付宝 CSV 中出现」(in_csv)，供独立 bean
编辑器只补入 CSV 缺失的记录、不重复已有账单。

用法（独立运行，不进 pipeline）：
    python 脚本/processor_alipay_yue.py

输出：整理后数据/支付宝余额/支付宝余额_{范围}.xlsx
"""
import io
import re
import sys
import glob
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from config import RAW_DIR, ACCOUNTS

ROOT_DIR   = Path(__file__).parent.parent
YUE_DIR    = RAW_DIR / "支付宝余额"
ALIPAY_DIR = RAW_DIR / "支付宝"
OUT_DIR    = ROOT_DIR / "整理后数据" / "支付宝余额"
ACCOUNTS_YUE = ACCOUNTS["alipay_yue"]

# PDF 明细表的列：流水号 时间 名称/备注 收入 支出 账户余额 资金渠道
STD_COLUMNS = ["流水号", "交易时间", "名称备注", "收入", "支出", "账户余额", "资金渠道", "金额(元)", "in_csv"]


def _clean(cell) -> str:
    """去掉单元格内的折行/空白（PDF 表格抽取会把一格拆成多行）。"""
    return re.sub(r"\s+", "", str(cell)) if cell is not None else ""


def _num(cell):
    s = _clean(cell).replace(",", "")
    return float(s) if re.match(r"^-?\d+\.?\d*$", s) else None


def parse_yue_pdf(pdf_path: str) -> pd.DataFrame:
    """解析《收支明细证明》PDF → DataFrame（一行一笔余额流水）。"""
    import pdfplumber

    rows = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                for r in table:
                    if not r or len(r) < 7:
                        continue
                    serial = _clean(r[0])
                    # 明细行的流水号以交易日期打头（20YYMMDD...），借此过滤表头/汇总行
                    if not re.match(r"^20\d{7,}", serial):
                        continue
                    raw_time = _clean(r[1])  # 形如 2026-05-2908:32:31（日期与时间粘连）
                    m = re.match(r"(20\d{2}-\d{2}-\d{2})(\d{2}:\d{2}:\d{2})?", raw_time)
                    if m:
                        dt = pd.to_datetime(f"{m.group(1)} {m.group(2) or '00:00:00'}")
                    else:
                        dt = pd.NaT
                    income  = _num(r[3])
                    expense = _num(r[4])
                    amount  = income if income else (expense if expense else 0.0)
                    rows.append({
                        "流水号":   serial,
                        "交易时间": dt,
                        "名称备注": _clean(r[2]),
                        "收入":     income,
                        "支出":     expense,
                        "账户余额": _num(r[5]),
                        "资金渠道": _clean(r[6]),
                        "金额(元)": amount,
                    })
    df = pd.DataFrame(rows)
    print(f"  解析明细 {len(df)} 条；时间解析失败 {df['交易时间'].isna().sum() if len(df) else 0} 条")
    return df


def _read_alipay_csv(path: str) -> pd.DataFrame | None:
    raw = open(path, encoding="gbk", errors="replace").read().splitlines()
    hi = [i for i, l in enumerate(raw) if "交易时间" in l and "金额" in l]
    if not hi:
        return None
    d = pd.read_csv(io.StringIO("\n".join(raw[hi[0]:])), engine="python")
    d.columns = [c.strip() for c in d.columns]
    return d


def build_existing_yue_postings() -> list[tuple]:
    """读取现有账本中余额账户的流水，作为账户侧去重基准。

    不能再用支付宝 CSV 的 (时间, |金额|)：同一笔余额⇄余额宝划转在 CSV
    里可能只有另一侧，绝对金额也会把两侧误判成同一笔。排除本数据源自身
    的 .bean 后，剩下的就是已由支付宝/基金编辑器生成的余额账户腿。
    """
    try:
        from beancount.loader import load_file
    except ImportError:
        return []
    try:
        entries, _, _ = load_file(str(ROOT_DIR / "bean_files" / "main.bean"))
    except Exception:
        return []
    result = []
    for entry in entries:
        if not hasattr(entry, "postings"):
            continue
        filename = str(entry.meta.get("filename", ""))
        if "支付宝余额" in filename:
            continue
        for posting in entry.postings:
            if posting.account != ACCOUNTS_YUE:
                continue
            try:
                amount = round(float(posting.units.number), 2)
            except (TypeError, ValueError):
                continue
            raw_time = entry.meta.get("time", "")
            ts = pd.to_datetime(raw_time, errors="coerce")
            result.append((entry.date, ts, amount))
    return result


def mark_in_csv(df: pd.DataFrame, existing: list[tuple]) -> pd.DataFrame:
    """按余额账户自身的日期、金额、时间一对一匹配已入账流水。"""
    used = set()
    flags = []
    for _, row in df.iterrows():
        ts = row["交易时间"]
        amount = round(float(row["金额(元)"]), 2)
        candidates = [
            (i, item) for i, item in enumerate(existing)
            if i not in used and item[0] == ts.date() and item[2] == amount
        ]
        if not candidates:
            flags.append(False)
            continue
        # PDF 与 CSV 通常只差几秒；没有时间元数据时退化为同日同额。
        timed = [
            (i, item) for i, item in candidates
            if pd.notna(item[1]) and abs(item[1] - ts) <= pd.Timedelta(seconds=30)
        ]
        i, _ = (timed or candidates)[0]
        used.add(i)
        flags.append(True)
    result = df.copy()
    result["in_csv"] = flags
    return result


def main():
    pdfs = sorted(glob.glob(str(YUE_DIR / "*" / "*.pdf")))
    if not pdfs:
        print(f"❌ 未找到余额明细 PDF：{YUE_DIR}/*/*.pdf")
        sys.exit(1)

    existing = build_existing_yue_postings()
    print(f"现有账本余额账户流水：{len(existing)} 条（用于账户侧去重）")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for pdf_path in pdfs:
        rng = Path(pdf_path).parent.name  # 目录名即覆盖范围，如 2102-2605
        print(f"\n处理余额明细 PDF：{Path(pdf_path).name}（范围 {rng}）")
        df = parse_yue_pdf(pdf_path)
        if df.empty:
            print("  ⚠ 未解析到任何明细，跳过")
            continue
        df = mark_in_csv(df, existing)
        df = df.sort_values("交易时间").reset_index(drop=True)
        n_miss = int((~df["in_csv"]).sum())
        print(f"  CSV 已含 {int(df['in_csv'].sum())} 条；CSV 缺失（待补）{n_miss} 条")

        out_path = OUT_DIR / f"支付宝余额_{rng}.xlsx"
        df[STD_COLUMNS].to_excel(out_path, index=False)
        print(f"  ✅ 输出：{out_path}")


if __name__ == "__main__":
    main()
