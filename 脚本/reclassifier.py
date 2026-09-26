#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
beancount 消费账户精细化分类脚本
=====================================
规则存放在同目录的 rules.yaml，账期从 config.py 读取。

用法:
    python reclassifier.py           # 实际修改文件
    python reclassifier.py --dry-run # 仅预览，不修改文件
"""

import argparse
import re
import sys
import shutil
from pathlib import Path
from collections import Counter

import yaml

from config import PERIOD

_SCRIPT_DIR = Path(__file__).parent

def load_rules() -> list:
    rule_path = _SCRIPT_DIR / "rules.yaml"
    with open(rule_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("rules", [])

RULES = load_rules()

def extract_meta(block: str) -> dict:
    meta = {}
    for key in ["payee_raw", "goods", "type", "source", "category"]:
        m = re.search(rf'^\s+{key}:\s+"(.*)"', block, re.MULTILINE)
        if m:
            meta[key] = m.group(1)
    header = block.split('\n')[0]
    hm = re.findall(r'"([^"]*)"', header)
    if hm:
        meta.setdefault("payee_raw", hm[0])
    # 从 Expenses 行提取金额，如: "  Expenses:Food   88.00 CNY"
    amount_m = re.search(r'^\s+Expenses(?::\w+)+\s+([\d,]+\.?\d*)\s+CNY', block, re.MULTILINE)
    if amount_m:
        meta["amount"] = float(amount_m.group(1).replace(',', ''))
    return meta

def _contains_any(text, keywords): return any(str(kw).lower() in text.lower() for kw in keywords)
def _not_contains_any(text, keywords): return not any(str(kw).lower() in text.lower() for kw in keywords)
def _match_re(text, pattern): return bool(re.search(pattern, text))

def match_rule(rule, meta):
    payee    = meta.get("payee_raw", "")
    goods    = meta.get("goods", "")
    typ      = meta.get("type", "")
    source   = meta.get("source", "")
    category = meta.get("category", "")   # 京东原始账单的品类字段（如「图书文娱」），其它来源多为空
    mode     = rule.get("match_mode", "any")

    def chk_payee(): return _contains_any(payee, rule["payee_contains"]) if "payee_contains" in rule else None
    def chk_goods(): return _contains_any(goods, rule["goods_contains"]) if "goods_contains" in rule else None
    def chk_goods_re(): return _match_re(goods, rule["goods_re"]) if "goods_re" in rule else None
    def chk_type(): return _contains_any(typ, rule["type_contains"]) if "type_contains" in rule else None
    def chk_source(): return _contains_any(source, rule["source_contains"]) if "source_contains" in rule else None
    def chk_category(): return _contains_any(category, rule["category_contains"]) if "category_contains" in rule else None
    def chk_payee_re(): return bool(re.fullmatch(rule["payee_re"], payee)) if "payee_re" in rule else None
    def check_excl(): return _not_contains_any(goods, rule["goods_not_contains"]) if "goods_not_contains" in rule else True

    defined = [v for v in (f() for f in [chk_payee, chk_goods, chk_goods_re, chk_type, chk_source, chk_category, chk_payee_re]) if v is not None]

    if mode == "any": result = any(defined) if defined else False
    elif mode == "ALL": result = all(defined) if defined else False
    elif mode == "payee_AND_goods": result = bool(chk_payee()) and bool(chk_goods())
    elif mode == "payee_AND_goods_re": result = bool(chk_payee()) and bool(chk_goods_re())
    elif mode == "type_AND_goods_re": result = bool(chk_type()) and bool(chk_goods_re())
    elif mode == "type_AND_payee_re": result = bool(chk_type()) and bool(chk_payee_re())
    elif mode == "category_AND_goods": result = bool(chk_category()) and bool(chk_goods())
    elif mode == "category_AND_goods_re": result = bool(chk_category()) and bool(chk_goods_re())
    elif mode == "goods_re": result = bool(chk_goods_re())
    else: result = False

    if not (result and check_excl()):
        return False
    # 金额条件（amount_lt / amount_gte），若 meta 里没有金额则跳过该条件
    if "amount_lt" in rule:
        if meta.get("amount") is None or meta["amount"] >= rule["amount_lt"]:
            return False
    if "amount_gte" in rule:
        if meta.get("amount") is None or meta["amount"] < rule["amount_gte"]:
            return False
    return True

def _classify_with_rule_idx(meta):
    """返回 (account, rule_index) 或 (None, None)。"""
    for i, rule in enumerate(RULES):
        if match_rule(rule, meta):
            return rule["new_account"], i
    return None, None


def classify(meta):
    account, _ = _classify_with_rule_idx(meta)
    return account

# 默认只改写 Expenses 腿；其余腿（Assets/Liabilities/Income/Equity）一律保留，
# 避免误伤银行/转账对端。个别规则可通过 overwrite_accounts 显式放开特定账户
# （如「个人代收付业务过渡账户」对端是 Equity:Transfer / Equity:Unknown）。
ACCOUNT_LINE_RE = re.compile(
    r'^(?P<indent>\s+)(?P<account>(?:Assets|Liabilities|Equity|Income|Expenses)(?::\w+)+)(?P<rest>.*)$'
)
DEFAULT_OVERWRITE = ("Expenses:",)

def process_bean_file(filepath, dry_run=False, rule_hits=None):
    content = filepath.read_text(encoding="utf-8")
    lines = content.splitlines(keepends=True)
    stats = {"replaced": 0, "details": []}
    new_lines = []
    i = 0

    while i < len(lines):
        line = lines[i]
        if re.match(r'^\d{4}-\d{2}-\d{2}\s+[*!]', line):
            block_start = i
            block_lines = [line]
            j = i + 1
            while j < len(lines):
                nxt = lines[j]
                if nxt.strip() == '' or re.match(r'^\d{4}-\d{2}-\d{2}', nxt) or re.match(r'^[a-zA-Z]', nxt):
                    break
                block_lines.append(nxt)
                j += 1
            block_str = ''.join(block_lines)
            meta = extract_meta(block_str)
            new_account, rule_idx = _classify_with_rule_idx(meta)
            if rule_hits is not None and rule_idx is not None:
                rule_hits[rule_idx] = rule_hits.get(rule_idx, 0) + 1
            # 本次匹配规则允许改写的账户前缀（默认仅 Expenses，可由 overwrite_accounts 扩展）
            overwrite = DEFAULT_OVERWRITE
            if rule_idx is not None:
                extra = RULES[rule_idx].get("overwrite_accounts")
                if extra:
                    if isinstance(extra, str):
                        extra = [extra]
                    overwrite = DEFAULT_OVERWRITE + tuple(extra)
            for bl in block_lines:
                m = ACCOUNT_LINE_RE.match(bl.rstrip('\n\r'))
                if m and new_account and m.group('account').startswith(overwrite):
                    new_lines.append(f"{m.group('indent')}{new_account}{m.group('rest')}\n")
                    stats["replaced"] += 1
                    stats["details"].append({"old": m.group("account"), "new": new_account, "payee": meta.get("payee_raw",""), "goods": meta.get("goods","")})
                else:
                    new_lines.append(bl if bl.endswith('\n') else bl + '\n')
            i = j
        else:
            new_lines.append(line)
            i += 1

    if not dry_run:
        shutil.copy2(filepath, filepath.with_suffix(".bean.bak"))
        filepath.write_text(''.join(new_lines), encoding="utf-8")

    return stats

def _collect_bean_files(target_dir: Path) -> list:
    return sorted(f for f in target_dir.glob("*.bean")
                  if f.name != "account.bean"
                  and not f.name.endswith(".bak")
                  and not f.name.startswith("alipay_funds_"))

def _process_dir(target_dir: Path, dry_run: bool, rule_hits=None) -> int:
    bean_files = _collect_bean_files(target_dir)
    if not bean_files:
        print(f"  [跳过] {target_dir.name} 下没有 .bean 文件")
        return 0
    total = 0
    for fp in bean_files:
        stats = process_bean_file(fp, dry_run=dry_run, rule_hits=rule_hits)
        if rule_hits is None:
            print(f"\n{'='*55}\n  [{target_dir.name}] {fp.name}   替换: {stats['replaced']} 条")
            counter = Counter(d["new"] for d in stats["details"])
            for acc, cnt in sorted(counter.items()):
                print(f"    {acc:<48} {cnt:>3} 条")
            if dry_run and stats["details"]:
                print(f"  变更明细:")
                for d in stats["details"]:
                    print(f"    {d['old']} → {d['new']}  ({d['payee'][:30]} / {d['goods'][:30]})")
        total += stats["replaced"]
    return total

def _print_stats(rule_hits: dict) -> None:
    total_rules = len(RULES)
    hit_count = len(rule_hits)
    zero_hit = [i for i in range(total_rules) if i not in rule_hits]
    hit_sorted = sorted(rule_hits.items(), key=lambda x: -x[1])

    print(f"\n{'='*60}")
    print(f"  规则覆盖率统计（共 {total_rules} 条规则）")
    print(f"  命中：{hit_count} 条  |  从未命中：{len(zero_hit)} 条")
    print(f"{'='*60}")
    print(f"  TOP 命中规则：")
    for idx, cnt in hit_sorted[:30]:
        desc = RULES[idx].get("desc", f"[规则#{idx+1}]")
        acc  = RULES[idx]["new_account"]
        print(f"    {cnt:>5} 次   {desc[:38]:<38}  → {acc}")
    if len(hit_sorted) > 30:
        print(f"    ...（还有 {len(hit_sorted)-30} 条已命中规则）")
    if zero_hit:
        print(f"\n  ⚠️  从未命中的规则（{len(zero_hit)} 条）：")
        for idx in zero_hit[:25]:
            desc = RULES[idx].get("desc", f"[规则#{idx+1}]")
            acc  = RULES[idx]["new_account"]
            print(f"    [#{idx+1:03}]  {desc[:42]:<42}  → {acc}")
        if len(zero_hit) > 25:
            print(f"    ...（还有 {len(zero_hit)-25} 条，详见 rules.yaml）")
    print(f"{'='*60}")


def main():
    parser = argparse.ArgumentParser(description="beancount 消费账户精细化分类")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅预览变更，不修改文件、不备份")
    parser.add_argument("--all", action="store_true",
                        help="处理 bean_files/ 下所有账期目录，而非仅当前 PERIOD")
    parser.add_argument("--stats", action="store_true",
                        help="统计各规则命中次数（扫描全部账期，不修改文件）")
    args = parser.parse_args()

    bean_root = Path("bean_files")

    # --stats 模式：扫描全部账期，打印覆盖率报告
    if args.stats:
        period_dirs = sorted(d for d in bean_root.iterdir() if d.is_dir())
        if not period_dirs:
            print(f"[错误] {bean_root} 下没有账期目录"); sys.exit(1)
        print(f"统计模式   规则数：{len(RULES)} 条   账期数：{len(period_dirs)}（不修改文件）")
        rule_hits: dict[int, int] = {}
        for d in period_dirs:
            _process_dir(d, dry_run=True, rule_hits=rule_hits)
        _print_stats(rule_hits)
        return

    mode_tag = "预览(dry-run)" if args.dry_run else "正式"

    if args.all:
        period_dirs = sorted(d for d in bean_root.iterdir() if d.is_dir())
        if not period_dirs:
            print(f"[错误] {bean_root} 下没有账期目录"); sys.exit(1)
        print(f"模式：{mode_tag}   规则数：{len(RULES)} 条   账期数：{len(period_dirs)}")
        grand_total = 0
        for d in period_dirs:
            grand_total += _process_dir(d, dry_run=args.dry_run)
        if args.dry_run:
            print(f"\n{'='*55}\n  📋 预览完成！全部账期共 {grand_total} 条待替换（未修改任何文件）")
        else:
            print(f"\n{'='*55}\n  ✅ 完成！全部账期共替换 {grand_total} 条")
    else:
        target_dir = bean_root / PERIOD
        if not target_dir.exists():
            print(f"[错误] 目录不存在: {target_dir}"); sys.exit(1)
        bean_files = _collect_bean_files(target_dir)
        if not bean_files:
            print(f"[错误] {target_dir} 下没有 .bean 文件"); sys.exit(1)
        print(f"处理周期：{PERIOD}   模式：{mode_tag}   规则数：{len(RULES)} 条（rules.yaml）   文件数：{len(bean_files)}")
        total = _process_dir(target_dir, dry_run=args.dry_run)
        if args.dry_run:
            print(f"\n{'='*55}\n  📋 预览完成！共 {total} 条待替换（未修改任何文件）")
        else:
            print(f"\n{'='*55}\n  ✅ 完成！共替换 {total} 条   备份: {target_dir}/*.bean.bak")

if __name__ == "__main__":
    main()