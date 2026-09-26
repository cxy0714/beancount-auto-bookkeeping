#!/usr/bin/env python3
"""收支方向守卫：揪出「收入流向却挂在支出腿」的分录（收入被误记成负支出）。

银行卡的 balance 断言只校验余额数字，对端腿是 Income 还是 Expense 它根本不管，
所以这类方向错误在银行卡侧同样会发生且断言查不出来。非银行卡资产（微信零钱/
零钱通/支付宝余额等）更没有断言兜底。本检查器专门扫这种错误。

合法的「收入挂支出腿」（冲减，不报警）：
  - AA 群收款（type=群收款）：替朋友垫付后收回份子钱，冲减原餐费 = 正确。
  - 各类退款/退货/退库/冲补：冲减原支出 = 正确。

其余 flow=收入 但对端是 Expenses 的，一律视为待复核的方向问题。

用法：
  python 脚本/check_direction.py            # 扫全部账期，列出问题
  python 脚本/check_direction.py --strict   # 有问题则以非零码退出（供 pipeline/CI 用）
"""
import sys
from pathlib import Path
from collections import defaultdict

# 合法冲减的白名单关键词（出现在 type / goods / narration 任一即视为退款类冲减）
REFUND_KW = ("退款", "退货", "退库", "冲补", "冲账", "-退")
# 合法冲减的 type（AA 群收款）
CONTRA_TYPES = ("群收款",)


def _is_legit_contra(typ: str, goods: str, narration: str) -> bool:
    if any(t in typ for t in CONTRA_TYPES):
        return True
    blob = f"{typ} {goods} {narration}"
    return any(kw in blob for kw in REFUND_KW)


def scan(bean_main: str = "bean_files/main.bean"):
    from beancount import loader
    from beancount.core import data

    entries, _, _ = loader.load_file(bean_main)
    problems = []
    for ent in entries:
        if not isinstance(ent, data.Transaction):
            continue
        if ent.meta.get("flow", "") != "收入":
            continue
        exp_legs = [p for p in ent.postings if p.account.startswith("Expenses")]
        if not exp_legs:
            continue
        typ = ent.meta.get("type", "")
        goods = str(ent.meta.get("goods", ""))
        if _is_legit_contra(typ, goods, ent.narration or ""):
            continue
        asset = next((p.account for p in ent.postings
                      if p.account.startswith(("Assets", "Liabilities"))), "")
        amt = sum(abs(float(p.units.number)) for p in exp_legs)
        problems.append({
            "date": ent.date, "amount": amt, "type": typ,
            "payee": ent.meta.get("payee_raw", ""), "goods": goods.replace(" ", ""),
            "asset": asset, "is_bank": "Bank" in asset,
            "accounts": ",".join(p.account.split(":")[-1] for p in exp_legs),
        })
    return problems


def main():
    strict = "--strict" in sys.argv
    problems = scan()
    if not problems:
        print("✅ 收支方向检查通过：没有「收入误挂支出腿」的分录。")
        return

    by_asset = defaultdict(list)
    for p in problems:
        by_asset["银行卡" if p["is_bank"] else "非银行卡资产"].append(p)

    total = sum(p["amount"] for p in problems)
    print(f"⚠️  发现 {len(problems)} 笔「收入流向却挂在支出腿」（疑似收入误记为负支出），合计 ¥{total:.0f}")
    print("（AA 群收款、退款/退库等合法冲减已自动排除）\n")
    for grp in ("银行卡", "非银行卡资产"):
        rows = by_asset.get(grp)
        if not rows:
            continue
        sub = sum(r["amount"] for r in rows)
        print(f"### {grp}  —  {len(rows)} 笔, ¥{sub:.0f}")
        for r in sorted(rows, key=lambda x: -x["amount"]):
            print(f"  ¥{r['amount']:>8.1f} {r['date']} {r['type'][:6]:6s} "
                  f"{r['payee'][:12]:12s} {r['goods'][:22]:22s} [{r['asset'].split(':')[-1]}] -> {r['accounts']}")
        print()

    if strict:
        sys.exit(1)


if __name__ == "__main__":
    main()
