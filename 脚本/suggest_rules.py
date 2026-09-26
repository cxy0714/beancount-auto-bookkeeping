#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unknown 聚类规则建议器
======================
扫描所有账期的 Expenses:Unknown / Income:Unknown / Equity:Unknown 条目，
按 (payee, goods, type) 文本相似度聚类（TF-IDF char n-gram + DBSCAN），
为每个簇生成一条可追加到 rules.yaml 的规则（带 k-NN 推荐账户）。

用法:
    python 脚本/suggest_rules.py                       # 终端打印 top 簇
    python 脚本/suggest_rules.py --min-cluster 3       # 调大簇阈值
    python 脚本/suggest_rules.py --eps 0.4             # 调小→簇更细分
    python 脚本/suggest_rules.py --write 脚本/rules_suggestions.yaml
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from bean_transactions import ROOT, Txn, iter_transactions

UNKNOWN_ACCOUNTS = {"Expenses:Unknown", "Income:Unknown", "Equity:Unknown"}


def _check_deps() -> None:
    try:
        import sklearn  # noqa: F401
    except ImportError:
        print("❌ 需要 scikit-learn：pip install scikit-learn", file=sys.stderr)
        sys.exit(1)


def _text_feature(t: Txn) -> str:
    return f"{t.payee} {t.goods} {t.type_}".strip()


def _common_substring(strings: list[str], min_len: int = 2) -> str:
    """从一组字符串中选出现频率 ≥ 50% 且权重最高（频率 × 长度）的 substring。"""
    strings = [s for s in strings if s]
    if not strings:
        return ""
    shortest = min(strings, key=len)
    if len(shortest) < min_len:
        return ""
    threshold = max(2, int(len(strings) * 0.5))
    best: tuple[int, str] = (0, "")
    for length in range(min_len, len(shortest) + 1):
        seen = set()
        for start in range(len(shortest) - length + 1):
            sub = shortest[start:start + length]
            if sub in seen:
                continue
            seen.add(sub)
            count = sum(1 for s in strings if sub in s)
            if count < threshold:
                continue
            score = count * length
            if score > best[0]:
                best = (score, sub)
    return best[1]


def cluster_unknowns(min_cluster: int, eps: float):
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.cluster import DBSCAN
    from sklearn.neighbors import NearestNeighbors

    all_txns = list(iter_transactions())
    unknowns = [t for t in all_txns if t.account in UNKNOWN_ACCOUNTS]
    labeled  = [t for t in all_txns
                if t.account.startswith("Expenses:")
                and t.account not in UNKNOWN_ACCOUNTS
                and _text_feature(t)]

    if not unknowns:
        print("✅ 没有 Unknown 条目，无需聚类。")
        return []

    print(f"扫描完成：共 {len(all_txns)} 条交易")
    print(f"  Unknown: {len(unknowns)} 条")
    print(f"  已标注：{len(labeled)} 条（用于 k-NN 账户推荐）")

    vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4), min_df=1)
    unk_texts = [_text_feature(t) for t in unknowns]
    lab_texts = [_text_feature(t) for t in labeled]
    vec.fit(unk_texts + lab_texts)
    X_unk = vec.transform(unk_texts)
    X_lab = vec.transform(lab_texts) if labeled else None

    db = DBSCAN(eps=eps, min_samples=min_cluster, metric="cosine")
    labels = db.fit_predict(X_unk)

    knn = None
    if labeled:
        knn = NearestNeighbors(n_neighbors=min(10, len(labeled)), metric="cosine")
        knn.fit(X_lab)

    clusters: dict[int, list[int]] = {}
    for idx, lab in enumerate(labels):
        clusters.setdefault(int(lab), []).append(idx)

    results = []
    for cid, indices in sorted(clusters.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        if cid == -1:
            continue
        members = [unknowns[i] for i in indices]
        common_payee = _common_substring([t.payee for t in members])
        common_goods = _common_substring([t.goods for t in members])

        suggested, confidence = None, 0.0
        if knn is not None:
            _, nn_idx = knn.kneighbors(X_unk[indices])
            votes: Counter = Counter()
            for row in nn_idx:
                for j in row:
                    votes[labeled[j].account] += 1
            if votes:
                top_acct, top_count = votes.most_common(1)[0]
                suggested = top_acct
                confidence = top_count / sum(votes.values())

        results.append({
            "cid": cid,
            "members": members,
            "common_payee": common_payee,
            "common_goods": common_goods,
            "suggested_account": suggested,
            "confidence": confidence,
        })

    # 还顺便统计 noise 数（DBSCAN 视为孤立点的 Unknown）
    noise_count = sum(1 for lab in labels if lab == -1)
    if noise_count:
        print(f"  ⚠️  {noise_count} 条 Unknown 未能形成簇（孤立点，需手动处理）")
    return results


def _format_yaml_rule(r: dict) -> str:
    payee, goods = r["common_payee"], r["common_goods"]
    desc = payee or goods or f"簇#{r['cid']}"
    lines = [f'  - desc: "{desc}"']
    if payee:
        lines.append(f'    payee_contains: ["{payee}"]')
    elif goods:
        lines.append(f'    goods_contains: ["{goods}"]')
    else:
        sample = list({m.payee for m in r["members"] if m.payee})[:3]
        if sample:
            quoted = ", ".join(f'"{s}"' for s in sample)
            lines.append(f"    payee_contains: [{quoted}]")
    acct = r["suggested_account"] or "Expenses:Unknown"
    comment = f"  # 置信度 {r['confidence']:.0%}，请人工确认" if r["suggested_account"] else "  # TODO: 手动指定账户"
    lines.append(f"    new_account: {acct}{comment}")
    return "\n".join(lines)


def _print_results(results: list[dict], top: int) -> None:
    print(f"\n{'='*64}")
    print(f"  发现 {len(results)} 个簇（按规模降序，显示前 {min(top, len(results))} 个）")
    print(f"{'='*64}\n")
    for r in results[:top]:
        members = r["members"]
        print(f"━━━ 簇 #{r['cid']}   {len(members)} 条 Unknown ━━━")
        if r["common_payee"]:
            print(f"  常见 payee 片段: 「{r['common_payee']}」")
        if r["common_goods"]:
            print(f"  常见 goods 片段: 「{r['common_goods']}」")
        if r["suggested_account"]:
            print(f"  k-NN 建议账户: {r['suggested_account']}  (置信度 {r['confidence']:.0%})")
        else:
            print(f"  k-NN 建议账户: (无足够标注数据)")
        print(f"  样本明细 (最多 5 条):")
        for m in members[:5]:
            amt = f"¥{m.amount:.2f}" if m.amount is not None else "-"
            print(f"    {m.date}  {m.payee[:20]:<20} / {m.goods[:25]:<25}  {amt:>9}")
            print(f"      → {m.file.relative_to(ROOT)}:{m.line}")
        print(f"\n  建议规则（追加到 rules.yaml）:")
        for ln in _format_yaml_rule(r).splitlines():
            print(f"    {ln}")
        print()


def main():
    parser = argparse.ArgumentParser(description="Unknown 条目聚类 + 规则建议")
    parser.add_argument("--min-cluster", type=int, default=2,
                        help="DBSCAN min_samples（最小簇大小），默认 2")
    parser.add_argument("--eps", type=float, default=0.5,
                        help="DBSCAN cosine 距离阈值，默认 0.5（调小→簇更细）")
    parser.add_argument("--top", type=int, default=30, help="终端只显示前 N 个簇")
    parser.add_argument("--write", type=str, default=None,
                        help="把全部建议规则写到指定 yaml 文件（不动 rules.yaml）")
    args = parser.parse_args()

    _check_deps()
    results = cluster_unknowns(args.min_cluster, args.eps)
    if not results:
        return

    _print_results(results, args.top)

    if args.write:
        out = Path(args.write)
        out.parent.mkdir(parents=True, exist_ok=True)
        header = (
            "# 由 suggest_rules.py 自动生成 - 人工 review 后合并到 rules.yaml\n"
            f"# 共 {len(results)} 条建议\n"
            "rules:\n"
        )
        out.write_text(
            header + "\n".join(_format_yaml_rule(r) for r in results) + "\n",
            encoding="utf-8",
        )
        print(f"\n📝 建议规则已写入：{out.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
