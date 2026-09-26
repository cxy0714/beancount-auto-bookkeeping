#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账户体系完整性自检
==================
静态校验"分类/支付映射与手动菜单里能产出的每个账户"都已在 account.bean
open，并校验账户层级不变量，最后跑一遍 bean-check。用于防止以下回归：
  - 映射里写错账户名（如曾经的 Beauty:Skincare）→ 静默记到不存在的账户
  - 给某个账户拆了子类后，父类又被直接记账（破坏报表聚合）

用法:
    python 脚本/verify_accounts.py        # 全部检查；任一失败退出码 1

检查项:
  [1] account.bean 自身能被 bean-check 加载
  [2] rules.yaml 每个 new_account 已 open
  [3] config.py 分类/支付映射的目标账户已 open
  [4] add_manual.py 菜单里的账户已 open
  [5] 层级不变量：任何"有子类的 Expenses/Income 父账户"都没有被直接记账
  [6] bean-check bean_files/main.bean 无错误
"""

import re
import sys
from pathlib import Path

import yaml

_SCRIPT_DIR = Path(__file__).resolve().parent
_ROOT = _SCRIPT_DIR.parent
ACCOUNT_BEAN = _ROOT / "bean_files" / "account.bean"
MAIN_BEAN = _ROOT / "bean_files" / "main.bean"
RULES_YAML = _SCRIPT_DIR / "rules.yaml"
ADD_MANUAL = _SCRIPT_DIR / "add_manual.py"

sys.path.insert(0, str(_SCRIPT_DIR))

_ACC_RE = re.compile(r'(?:Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z0-9\-]+)+')
_POSTING_RE = re.compile(
    r'^\s+((?:Assets|Liabilities|Equity|Income|Expenses)(?::[A-Za-z0-9\-]+)+)'
)

# 平账/清算父账户允许名单：父级本身即兜底语义时，允许"父级有子类却被直接记账"。
# 目前为空——Equity:Transfer 已拆出 :Misc/:Credit 叶子，父级不再直接记账；
# 币种账户 1001/9001/Offline 同样已拆出 :CNY 叶子。父级若被直接记账即视为违规。
INVARIANT_ALLOW: set[str] = set()


def load_opened() -> set[str]:
    opened = set()
    for line in ACCOUNT_BEAN.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^\d{4}-\d{2}-\d{2}\s+open\s+([A-Za-z0-9:\-]+)', line)
        if m:
            opened.add(m.group(1))
    return opened


def targets_from_rules() -> dict[str, str]:
    """new_account → 来源描述。"""
    data = yaml.safe_load(RULES_YAML.read_text(encoding="utf-8"))
    out = {}
    for r in data.get("rules", []):
        acc = r.get("new_account")
        if acc:
            out.setdefault(acc, f"rules.yaml «{r.get('desc', '?')[:24]}»")
    return out


def targets_from_config() -> dict[str, str]:
    import config
    out: dict[str, str] = {}

    def add(acc, where):
        if isinstance(acc, str) and _ACC_RE.fullmatch(acc):
            out.setdefault(acc, where)

    for v in config.ALIPAY_EXPENSE_MAP.values():
        add(v, "config.ALIPAY_EXPENSE_MAP")
    for _, v in config.JINGDONG_EXPENSE_MAP_KEYWORDS:
        add(v, "config.JINGDONG_EXPENSE_MAP")
    add(config.JINGDONG_EXPENSE_DEFAULT, "config.JINGDONG_EXPENSE_DEFAULT")
    for _, v in config.PAYMENT_RULES_ALL:
        add(v, "config.PAYMENT_RULES")
    return out


def targets_from_add_manual() -> dict[str, str]:
    out = {}
    for acc in _ACC_RE.findall(ADD_MANUAL.read_text(encoding="utf-8")):
        out.setdefault(acc, "add_manual.py 菜单")
    return out


def posting_usage() -> dict[str, int]:
    usage: dict[str, int] = {}
    for fp in _ROOT.glob("bean_files/**/*.bean"):
        if fp.name == "account.bean" or fp.name.endswith(".bak"):
            continue
        if ".backups" in fp.parts or "tmp" in fp.parts:
            continue
        for line in fp.read_text(encoding="utf-8").splitlines():
            m = _POSTING_RE.match(line)
            if m:
                usage[m.group(1)] = usage.get(m.group(1), 0) + 1
    return usage


def check_targets_opened(opened: set[str]) -> list[str]:
    """返回违规描述列表（目标账户未 open）。"""
    all_targets: dict[str, str] = {}
    for src in (targets_from_rules(), targets_from_config(), targets_from_add_manual()):
        for acc, where in src.items():
            all_targets.setdefault(acc, where)
    bad = []
    for acc, where in sorted(all_targets.items()):
        if acc.startswith("Assets:Invest:Fund:FUND"):   # 基金代码动态注册
            continue
        if acc not in opened:
            bad.append(f"{acc}   ←  {where}")
    return bad


def check_hierarchy_invariant(opened: set[str], usage: dict[str, int]) -> list[str]:
    """任何已 open 且有子类的父账户都不得被直接记账（允许名单除外）。
    覆盖 Expenses/Income 的 :Misc 以及 Assets/Liabilities 的币种 :CNY/:EUR 等。"""
    parents = {
        a for a in opened
        if any(b != a and b.startswith(a + ":") for b in opened)
    }
    bad = []
    for p in sorted(parents):
        if p in INVARIANT_ALLOW:
            continue
        n = usage.get(p, 0)
        if n > 0:
            bad.append(f"{p}   ←  被直接记账 {n} 次（应改记其子类，如 :Misc / :CNY）")
    return bad


def run_bean_check() -> tuple[bool, list[str]]:
    try:
        from beancount import loader
    except ImportError:
        return True, ["（beancount 未安装，跳过 bean-check）"]
    _, errors, _ = loader.load_file(str(MAIN_BEAN))
    msgs = [f"{type(e).__name__}: {getattr(e, 'message', e)}" for e in errors]
    return len(errors) == 0, msgs


def main() -> None:
    opened = load_opened()
    usage = posting_usage()
    failures = 0

    print("=" * 64)
    print("账户体系完整性自检  verify_accounts.py")
    print("=" * 64)

    # [2][3][4] 目标账户已 open
    bad_targets = check_targets_opened(opened)
    if bad_targets:
        failures += 1
        print(f"\n❌ 映射/菜单目标账户未在 account.bean open（{len(bad_targets)} 个）:")
        for b in bad_targets:
            print(f"    {b}")
    else:
        print("\n✔ 所有映射/菜单目标账户均已 open")

    # [5] 层级不变量
    bad_inv = check_hierarchy_invariant(opened, usage)
    if bad_inv:
        failures += 1
        print(f"\n❌ 父账户被直接记账（{len(bad_inv)} 个）:")
        for b in bad_inv:
            print(f"    {b}")
    else:
        print("✔ 层级不变量成立：所有有子类的父账户（含币种 :CNY）均未被直接记账")

    # [6] bean-check
    ok, msgs = run_bean_check()
    if ok:
        print(f"✔ bean-check 通过  {msgs[0] if msgs else ''}".rstrip())
    else:
        failures += 1
        print(f"\n❌ bean-check 失败（{len(msgs)} 条）:")
        for m in msgs[:15]:
            print(f"    {m}")

    print("\n" + "=" * 64)
    if failures:
        print(f"结果：❌ {failures} 项检查未通过")
        sys.exit(1)
    print("结果：✅ 全部通过")


if __name__ == "__main__":
    main()
