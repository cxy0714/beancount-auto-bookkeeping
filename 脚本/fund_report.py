#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
基金 XIRR 月度报告
==================
读取 main.bean，提取每只基金的现金流，从天天基金 API 拉取最新单位净值，
计算 XIRR + 关键指标，输出 markdown 到 reports/fund_report_{PERIOD}.md。

用法:
    python 脚本/fund_report.py                  # 用 config.py 里的 PERIOD
    python 脚本/fund_report.py --period 2605
    python 脚本/fund_report.py --no-fetch       # 不联网，用缓存净值
"""

import argparse
import json
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR = SCRIPT_DIR.parent
sys.path.insert(0, str(SCRIPT_DIR))

import config  # noqa: E402  (PERIOD)

from beancount import loader  # noqa: E402
from beancount.core import data  # noqa: E402


# ────────────────────────────────────────────────────────────
# 数据提取
# ────────────────────────────────────────────────────────────
def load_fund_names(entries):
    names = {}
    for e in entries:
        if isinstance(e, data.Commodity) and e.currency.startswith("FUND"):
            names[e.currency] = (e.meta or {}).get("name", e.currency)
    return names


def extract_cashflows(entries):
    """
    返回 {symbol: [(date, signed_cashflow, kind)]} 和 {symbol: initial_date}
    投资者视角：买入/初始 = 负值（钱出去），卖出/分红 = 正值（钱回来）
    """
    flows = defaultdict(list)
    initial_dates = {}
    for e in entries:
        if not isinstance(e, data.Transaction):
            continue
        fund_postings = [
            p for p in e.postings
            if p.account.startswith("Assets:Invest:Fund:FUND") and p.units
        ]
        if not fund_postings:
            continue
        is_initial = bool(e.narration) and "初始持仓" in e.narration
        for fp in fund_postings:
            sym = fp.units.currency
            n = fp.units.number
            if is_initial:
                initial_dates[sym] = e.date
            if n is None:
                # 现金分红：FUND 占位为空，从钱包侧拿到账金额
                for p in e.postings:
                    if (p.account.startswith("Assets:Cash:")
                            and p.units and p.units.currency == "CNY"
                            and p.units.number > 0):
                        flows[sym].append((e.date, float(p.units.number), "dividend"))
                        break
            elif n > 0 and fp.cost:
                kind = "initial" if is_initial else "buy"
                flows[sym].append((e.date, -float(n * fp.cost.number), kind))
            elif n < 0 and fp.price:
                flows[sym].append((e.date, float(-n * fp.price.number), "sell"))
    return flows, initial_dates


def compute_holdings(entries):
    h = defaultdict(float)
    for e in entries:
        if not isinstance(e, data.Transaction):
            continue
        for p in e.postings:
            if (p.account.startswith("Assets:Invest:Fund:FUND")
                    and p.units and p.units.number is not None):
                h[p.units.currency] += float(p.units.number)
    return h


# ────────────────────────────────────────────────────────────
# 天天基金 净值
# ────────────────────────────────────────────────────────────
def fetch_nav(fund_code, timeout=8):
    """fund_code 是 6 位数字，如 '006479'。返回 dict 或 None（附 error 供打印）。

    使用天天基金 f10/lsjz 历史净值接口，取最近一条已确认净值：
      {"Data": {"LSJZList": [{"FSRQ": "2026-07-22", "DWJZ": "7.9240", ...}]}}
    该接口只有已确认净值，没有盘中实时估值（estimate 字段因此恒为 None）。
    """
    url = (config.FUND_NAV_API_URL.format(fund_code=fund_code)
           + f"&rt={int(datetime.now().timestamp() * 1000)}")
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "http://fund.eastmoney.com/",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", errors="replace")
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    try:
        d = json.loads(txt)
    except json.JSONDecodeError:
        return {"error": f"响应非 JSON（前 60 字：{txt.strip()[:60]!r}）"}

    lst = ((d.get("Data") or {}).get("LSJZList")) or []
    if not lst:
        return {"error": f"LSJZList 为空（ErrCode={d.get('ErrCode')} ErrMsg={d.get('ErrMsg')!r}）"}

    row = lst[0]
    nav_raw = row.get("DWJZ")
    if nav_raw in (None, "", "-"):
        return {"error": f"DWJZ 缺失（FSRQ={row.get('FSRQ')!r}）"}
    try:
        nav_val = float(nav_raw)
    except (TypeError, ValueError):
        return {"error": f"DWJZ 非数值：{nav_raw!r}"}
    if nav_val <= 0:
        return {"error": f"DWJZ 非正：{nav_val}"}

    return {
        "nav": nav_val,
        "nav_date": row.get("FSRQ"),
        "name": None,          # lsjz 接口不返回基金名；报告用 ledger meta 里的名字
        "estimate": None,      # lsjz 无盘中实时估值
        "estimate_pct": None,
    }


# ────────────────────────────────────────────────────────────
# XIRR
# ────────────────────────────────────────────────────────────
def xirr(cashflows):
    """cashflows = [(date, signed_amount)]，找年化复利收益率。"""
    if len(cashflows) < 2:
        return None
    has_pos = any(c > 0 for _, c in cashflows)
    has_neg = any(c < 0 for _, c in cashflows)
    if not (has_pos and has_neg):
        return None
    try:
        from scipy.optimize import brentq, newton
    except ImportError:
        return None
    t0 = min(d for d, _ in cashflows)

    def npv(r):
        if r <= -0.9999:
            return float("inf")
        return sum(cf / (1 + r) ** ((d - t0).days / 365.0) for d, cf in cashflows)

    # 优先 brentq，逐级放宽区间
    for hi in (10, 50, 200, 1000):
        try:
            lo_val = npv(-0.99)
            hi_val = npv(hi)
            if lo_val == float("inf") or hi_val == float("inf"):
                continue
            if lo_val * hi_val < 0:
                return brentq(npv, -0.99, hi, xtol=1e-7, maxiter=200)
        except Exception:
            continue

    # Newton 法兜底
    for guess in (0.1, 0.5, -0.1, -0.5, 1.0):
        try:
            res = newton(npv, guess, maxiter=100, tol=1e-7)
            if res is not None and not (res != res) and res > -0.9999:  # NaN check
                return float(res)
        except Exception:
            continue

    return None


# ────────────────────────────────────────────────────────────
# 从已有 prices.bean 读取最新虚拟/历史净值
# ────────────────────────────────────────────────────────────
def load_latest_prices_from_bean(path=None):
    """返回 {symbol: {"nav": float, "nav_date": "YYYY-MM-DD"}}。

    仅供离线演示/网络抓取失败时回退使用，取每个基金日期最新的 price 行。
    """
    if path is None:
        path = ROOT_DIR / "bean_files" / "prices.bean"
    if not path.exists():
        return {}
    price_re = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+price\s+(\S+)\s+([\d.]+)\s+CNY")
    out = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        m = price_re.match(ln.strip())
        if not m:
            continue
        d, sym, nav = m.group(1), m.group(2), float(m.group(3))
        if sym not in out or d > out[sym]["nav_date"]:
            out[sym] = {"nav": nav, "nav_date": d}
    return out


# ────────────────────────────────────────────────────────────
# price 指令（供 fava 按市值估值）
# ────────────────────────────────────────────────────────────
def write_prices_bean(prices, holdings, path=None):
    """把在持基金的最新净值并入 beancount price 指令（累积历史，不覆盖）。

    main.bean include 了 bean_files/prices.bean，没有 price 时 fava
    净资产按成本计，基金涨跌完全不体现。
    每次运行把本次净值按 (净值日, 代码) 并入：同日同基金则更新，新日期则
    追加，旧净值一律保留——这样长期累积出净值序列，fava 才能正确画历史市值曲线。
    已清仓基金的历史净值也保留（不影响估值，但留作历史）。
    """
    if path is None:
        path = ROOT_DIR / "bean_files" / "prices.bean"

    # 1) 解析现有 price 行，键 (净值日, 代码) → 净值字符串，保留全部历史
    PRICE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+price\s+(\S+)\s+([\d.]+)\s+CNY")
    merged = {}  # (date, sym) -> nav_str(4位)
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            m = PRICE_RE.match(ln.strip())
            if m:
                merged[(m.group(1), m.group(2))] = m.group(3)

    # 2) 并入本次在持基金的最新净值（同 (日,代码) 覆盖更新）
    n = 0
    for sym in sorted(holdings):
        if holdings[sym] <= 1e-4:
            continue
        info = prices.get(sym) or {}
        nav, nav_date = info.get("nav"), info.get("nav_date")
        if not nav or not nav_date:
            continue
        merged[(nav_date, sym)] = f"{nav:.4f}"
        n += 1
    if n == 0:
        return 0  # 本次全部抓取失败：保留旧文件不动，宁可净值过期也不清空

    # 3) 按 (日期, 代码) 排序写回，累积全部历史净值点
    header = [
        "; ===== 基金净值（fund_report.py 自动生成，合并累积历史，勿手改）=====",
        "; 供 fava 按市值估值：无 price 时净资产按成本计，基金涨跌不体现",
        "; 每次运行按 (净值日,代码) 并入新净值并保留旧值，逐步沉淀净值序列",
    ]
    body = [
        f"{d} price {sym}   {nav} CNY"
        for (d, sym), nav in sorted(merged.items())
    ]
    path.write_text("\n".join(header + body) + "\n", encoding="utf-8")
    return n


# ────────────────────────────────────────────────────────────
# 报告生成
# ────────────────────────────────────────────────────────────
def format_md(period, prices, flows_by_sym, holdings, fund_names, initial_dates):
    today = date.today()
    lines = []
    lines.append(f"# 基金 XIRR 报告 ({period})")
    lines.append("")
    lines.append(f"生成时间：{datetime.now():%Y-%m-%d %H:%M}")
    lines.append("")

    rows = []
    portfolio_flows = []
    for sym, raw in sorted(flows_by_sym.items()):
        cfs = sorted([(d, c) for d, c, _ in raw])
        invested = sum(-c for d, c in cfs if c < 0)
        received = sum(c for d, c in cfs if c > 0)
        held = holdings.get(sym, 0.0)
        nav_info = prices.get(sym) or {}
        nav = nav_info.get("nav")
        market = held * nav if (nav and held > 0.001) else 0.0
        cfs_xirr = list(cfs)
        nav_missing_in_position = held > 0.001 and not nav
        if held > 0.001 and nav:
            cfs_xirr.append((today, market))
        pnl = received + market - invested
        ret_pct = (pnl / invested * 100) if invested else 0
        # 缺净值的在持基金：单只 XIRR 算不准，组合 XIRR 也不能加入它
        r = None if nav_missing_in_position else xirr(cfs_xirr)
        rows.append({
            "sym": sym,
            "name": fund_names.get(sym, sym),
            "held": held,
            "invested": invested,
            "received": received,
            "market": market,
            "pnl": pnl,
            "ret_pct": ret_pct,
            "xirr": r,
            "nav": nav,
            "nav_date": nav_info.get("nav_date"),
            "has_initial": sym in initial_dates,
            "first_date": cfs[0][0] if cfs else None,
            "nav_missing": nav_missing_in_position,
        })
        if not nav_missing_in_position:
            portfolio_flows.extend(cfs_xirr)

    portfolio_xirr = xirr(portfolio_flows)
    nav_missing_count = sum(1 for r in rows if r.get("nav_missing"))
    total_invested = sum(r["invested"] for r in rows)
    total_received = sum(r["received"] for r in rows)
    total_market = sum(r["market"] for r in rows)
    total_pnl = total_received + total_market - total_invested
    total_ret = (total_pnl / total_invested * 100) if total_invested else 0

    lines.append("## 组合总览")
    lines.append("")
    lines.append(f"- 累计投入：¥{total_invested:,.2f}")
    lines.append(f"- 已收回（赎回 + 现金分红）：¥{total_received:,.2f}")
    lines.append(f"- 当前持仓市值：¥{total_market:,.2f}")
    lines.append(f"- 净盈亏：**¥{total_pnl:+,.2f}**（{total_ret:+.2f}%）")
    if portfolio_xirr is not None:
        suffix = ""
        if nav_missing_count:
            suffix = f"  ⚠️ 仅含有净值的 {len(rows) - nav_missing_count}/{len(rows)} 只基金"
        lines.append(f"- 组合 XIRR（年化）：**{portfolio_xirr * 100:+.2f}%**{suffix}")
    else:
        lines.append("- 组合 XIRR：N/A")
    lines.append("")

    lines.append("## 分基金明细")
    lines.append("")
    lines.append("| 基金 | 状态 | 投入 | 已收回 | 当前市值 | 净盈亏 | 总收益率 | XIRR | 净值日期 | 注 |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---:|---|---|")
    rows_sorted = sorted(rows, key=lambda x: -(x["xirr"] if x["xirr"] is not None else -99))
    for r in rows_sorted:
        status = "在持" if r["held"] > 0.001 else "已清仓"
        xirr_str = f"{r['xirr'] * 100:+.2f}%" if r["xirr"] is not None else "N/A"
        note = []
        if r["held"] > 0.001 and not r["nav"]:
            note.append("❌缺净值")
        nstr = "；".join(note) or "-"
        lines.append(
            f"| {r['sym']} {r['name']} | {status} "
            f"| ¥{r['invested']:.2f} | ¥{r['received']:.2f} | ¥{r['market']:.2f} "
            f"| ¥{r['pnl']:+.2f} | {r['ret_pct']:+.2f}% | **{xirr_str}** "
            f"| {r['nav_date'] or '-'} | {nstr} |"
        )
    lines.append("")

    if total_market > 0.01:
        lines.append("## 当前资产配置")
        lines.append("")
        lines.append("```")
        for r in sorted(rows, key=lambda x: -x["market"]):
            if r["market"] < 0.01:
                continue
            pct = r["market"] / total_market * 100
            bar = "█" * int(pct / 2)
            lines.append(
                f"  {r['sym']} {r['name'][:18]:<18} ¥{r['market']:>9.2f}  "
                f"{pct:>5.2f}%  {bar}"
            )
        lines.append("```")
        lines.append("")

    lines.append("## 简评")
    lines.append("")
    notes = []
    if portfolio_xirr is not None:
        if portfolio_xirr > 0.15:
            notes.append(f"✅ 组合年化 {portfolio_xirr * 100:+.2f}%，>15%，目前表现优秀；注意市场风格切换。")
        elif portfolio_xirr > 0.05:
            notes.append(f"组合年化 {portfolio_xirr * 100:+.2f}%，跑赢余额宝（~2%）。")
        elif portfolio_xirr > 0:
            notes.append(f"⚠️ 组合年化 {portfolio_xirr * 100:+.2f}%，正但偏低，性价比一般。")
        else:
            notes.append(f"❌ 组合年化 {portfolio_xirr * 100:+.2f}%，亏损中，建议复盘配置。")
    if total_market > 0:
        top = max(rows, key=lambda x: x["market"])
        top_pct = top["market"] / total_market * 100
        if top_pct > 40:
            notes.append(f"⚠️ 集中度高：**{top['name']}** 占 {top_pct:.1f}%，单一品种暴露较大。")
    bad_held = [r for r in rows if r["held"] > 0.001 and r["xirr"] is not None and r["xirr"] < -0.10]
    if bad_held:
        names = "、".join(f"{r['name']}({r['xirr'] * 100:+.1f}%)" for r in bad_held)
        notes.append(f"❌ 年化 < -10% 的在持品种：{names}，考虑是否止损。")
    good_held = [r for r in rows if r["held"] > 0.001 and r["xirr"] is not None and r["xirr"] > 0.20]
    if good_held:
        names = "、".join(f"{r['name']}({r['xirr'] * 100:+.1f}%)" for r in good_held)
        notes.append(f"✅ 年化 > 20% 的在持品种：{names}（注意均值回归风险）。")
    no_nav = [r for r in rows if r["held"] > 0.001 and not r["nav"]]
    if no_nav:
        notes.append(f"⚠️ 缺净值（接口失败或代码不对）：{'、'.join(r['sym'] for r in no_nav)}，估值未计入。")
    for n in notes:
        lines.append(f"- {n}")
    if not notes:
        lines.append("- 暂无突出提示。")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("**指标说明**")
    lines.append("")
    lines.append(
        "- **XIRR (年化)**：扩展内部收益率。综合考虑每笔现金流的时点，找出一个固定年利率 r，"
        "使得所有现金流按 (1+r)^(在场年数) 折算后求和等于 0。是评估不规则定投真实回报的标准指标。"
    )
    lines.append("- **总收益率** = 净盈亏 / 累计投入。不考虑时间，只看绝对盈亏比例。")
    lines.append(
        "- **净值来源**：天天基金 `fundgz` 接口（`dwjz` = 最新单位净值，每个交易日 18:00 后更新）。"
        "失败时回落到 `reports/prices_cache.json` 缓存。"
    )
    lines.append(
        "- **初始持仓**：2025-08-21 之前的持仓在 bean 中被压缩为当日一笔现金流，"
        "因此真实买入时点早于 2025-08-21 的基金，其 XIRR 会略偏乐观（在场时间被低估）。"
        "随着持续向前回溯账单，该偏差会自然消失。"
    )

    return "\n".join(lines) + "\n"


# ────────────────────────────────────────────────────────────
# 入口
# ────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default=None)
    parser.add_argument("--no-fetch", action="store_true",
                        help="不联网，使用 reports/prices_cache.json")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    # 提前检测 scipy；缺失时大声警告而不是静默让 XIRR 全部 None
    try:
        import scipy.optimize  # noqa: F401
    except ImportError:
        print("❌ 缺少依赖 scipy，所有基金的 XIRR 将显示 N/A。", file=sys.stderr)
        print("   请运行：pip install scipy", file=sys.stderr)
        print("   （继续生成报告，但 XIRR 列会全为空）\n", file=sys.stderr)

    period = args.period or config.PERIOD

    main_bean = ROOT_DIR / "bean_files" / "main.bean"
    print(f"📖 加载 {main_bean.relative_to(ROOT_DIR)}")
    entries, errors, _ = loader.load_file(str(main_bean))
    if errors:
        print(f"⚠️ bean 加载有 {len(errors)} 个错误：", file=sys.stderr)
        for e in errors[:3]:
            print("  -", getattr(e, "message", str(e))[:200], file=sys.stderr)

    fund_names = load_fund_names(entries)
    flows_by_sym, initial_dates = extract_cashflows(entries)
    holdings = compute_holdings(entries)
    print(f"🔍 提取到 {len(flows_by_sym)} 只基金的现金流")

    reports_dir = ROOT_DIR / "reports"
    reports_dir.mkdir(exist_ok=True)
    cache_path = reports_dir / "prices_cache.json"

    prices = {}
    if not args.no_fetch:
        print("🌐 从天天基金抓取最新净值...")
        for sym in sorted(flows_by_sym):
            code = sym.replace("FUND", "")
            info = fetch_nav(code)
            if info and info.get("nav"):
                prices[sym] = info
                nm = (info.get("name") or "")[:20]
                print(f"  ✓ {sym}  {nm:<20}  ¥{info['nav']:.4f}  ({info['nav_date']})")
            else:
                err = (info or {}).get("error", "未知错误")
                print(f"  ✗ {sym} 抓取失败（{err}），尝试用缓存")
        if cache_path.exists():
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            for sym, c in cached.items():
                prices.setdefault(sym, c)
        cache_path.write_text(
            json.dumps(prices, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    else:
        if cache_path.exists():
            prices = json.loads(cache_path.read_text(encoding="utf-8"))
            print(f"📂 使用缓存净值：{cache_path.relative_to(ROOT_DIR)}")
        else:
            print("⚠️ --no-fetch 但没有缓存，市值/XIRR 将不准")

    # 离线/抓取失败时，回退到 bean_files/prices.bean 中已有的最新净值
    bean_prices = load_latest_prices_from_bean()
    for sym, info in bean_prices.items():
        if sym not in prices:
            prices[sym] = info
    if bean_prices:
        print(f"📂 使用 bean_files/prices.bean 中的已有净值：{len(bean_prices)} 只")

    n_prices = write_prices_bean(prices, holdings)
    if n_prices:
        print(f"💹 已更新 bean_files/prices.bean（{n_prices} 只在持基金净值）")
    else:
        print("⚠️ 无可用净值，保留 bean_files/prices.bean 旧值不动")

    md = format_md(period, prices, flows_by_sym, holdings, fund_names, initial_dates)

    out_path = (Path(args.output) if args.output
                else reports_dir / f"fund_report_{period}.md")
    out_path.write_text(md, encoding="utf-8")
    print(f"\n📄 报告已写入：{out_path.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
