#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账本自动化流水线
================
用法:
    python pipeline.py 2512                          # 指定账期，跑全量
    python pipeline.py                               # 交互输入
    python pipeline.py --period 2603
    python pipeline.py 2507-2508 --only credit_card  # 仅跑信用卡相关步骤
    python pipeline.py 2603 --only alipay,wechat     # 多 source 用逗号分隔

--only 支持的 source: alipay / bank / wechat / jingdong / credit_card /
                      reclassifier / fund_report

工作原理：
    直接修改 config.py 里的 PERIOD 值，然后依次调用各模块的 main()。
    不再使用 token patch 方案。
"""

import sys
import os
import datetime
import importlib
import importlib.util
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
ROOT_DIR   = SCRIPT_DIR.parent

# 流水线顺序（不含扩展名）
PIPELINE = [
    "processor_alipay",
    "processor_bank",
    "processor_bank_bankb",
    "processor_bank_bankc",
    "processor_wechat",
    "processor_jingdong",
    "processor_creditcard",
    "bean_edit_alipay",
    "bean_edit_alipay_funds",
    "bean_edit_bank",
    "bean_edit_wechat",
    "bean_edit_jingdong",
    "bean_edit_creditcard",
    "reclassifier",
    "merge_bean_headers",
    "fund_report",
    "verify_accounts",
]

# 上游失败时应跳过的下游步骤（key=上游, value=需跳过的下游列表）
# bank 系列任一处理器产出的 Excel 都由 bean_edit_bank 统一消费，
# 因此只在两边处理器都失败时才跳过 bean_edit_bank（其内部按卡逐张读取，缺一不阻塞）。
SKIP_ON_FAILURE = {
    "processor_alipay":       ["bean_edit_alipay", "bean_edit_alipay_funds"],
    "processor_wechat":       ["bean_edit_wechat"],
    "processor_jingdong":     ["bean_edit_jingdong"],
    "processor_creditcard":   ["bean_edit_creditcard"],
}

# --only 的 source 名称 → 包含的步骤列表
SOURCE_STEPS = {
    "alipay":      ["processor_alipay", "bean_edit_alipay", "bean_edit_alipay_funds"],
    "bank":        ["processor_bank", "processor_bank_bankb", "processor_bank_bankc", "bean_edit_bank"],
    "wechat":      ["processor_wechat", "bean_edit_wechat"],
    "jingdong":    ["processor_jingdong", "bean_edit_jingdong"],
    "credit_card": ["processor_creditcard", "bean_edit_creditcard"],
    "reclassifier": ["reclassifier"],
    "fund_report": ["fund_report"],
    "verify": ["verify_accounts"],
}


REQUIRED_PACKAGES = ["yaml", "pandas", "openpyxl"]
PDF_PACKAGES      = ["pdfplumber"]   # 仅 processor_alipay 需要，不可用时只警告
OPTIONAL_PACKAGES = [("scipy", "scipy", "fund_report XIRR")]  # (import名, pip名, 用途)

def check_dependencies():
    missing, missing_pdf = [], []
    for pkg in REQUIRED_PACKAGES:
        try:
            __import__(pkg)
        except Exception:
            missing.append(pkg)
    for pkg in PDF_PACKAGES:
        try:
            __import__(pkg)
        except BaseException:
            missing_pdf.append(pkg)
    for import_name, pip_name, desc in OPTIONAL_PACKAGES:
        try:
            __import__(import_name)
        except Exception:
            print(f"⚠️  {pip_name} 不可用，{desc} 将显示 N/A（运行：pip install {pip_name}）")
    if missing_pdf:
        print("\u26a0\ufe0f  pdfplumber 不可用，processor_alipay 步骤将失败（其余步骤继续）")
    if missing:
        pip_names = {"yaml": "pyyaml"}
        names = [pip_names.get(p, p) for p in missing]
        print(f"\u274c 缺少关键依赖：{', '.join(names)}")
        print(f"   请运行：pip install {' '.join(names)}")
        sys.exit(1)


def count_unknowns(period: str) -> dict[str, int]:
    """统计当期 bean 文件中各 Unknown 账户出现次数。"""
    bean_dir = ROOT_DIR / "bean_files" / period
    counts: dict[str, int] = {}
    if not bean_dir.exists():
        return counts
    for f in sorted(bean_dir.glob("*.bean")):
        if f.name.endswith(".bak"):
            continue
        text = f.read_text(encoding="utf-8")
        for acct in ["Expenses:Unknown", "Income:Unknown", "Equity:Unknown"]:
            n = text.count(acct)
            if n:
                counts[acct] = counts.get(acct, 0) + n
    return counts


def load_module(name: str):
    """从脚本目录动态加载模块。"""
    path = SCRIPT_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod  = importlib.util.module_from_spec(spec)
    return spec, mod, path


def set_period(period: str):
    """将 config.py 里的 PERIOD 改为指定值（直接写文件）。"""
    config_path = SCRIPT_DIR / "config.py"
    text = config_path.read_text(encoding="utf-8")
    import re
    new_text = re.sub(
        r'^(PERIOD\s*=\s*)["\'].*?["\']',
        rf'\g<1>"{period}"',
        text,
        flags=re.MULTILINE
    )
    config_path.write_text(new_text, encoding="utf-8")
    print(f"  config.py PERIOD -> {period}")


def run_module(name: str) -> tuple[int, str, str]:
    """执行模块的 main()，捕获 stdout/stderr。"""
    import io, contextlib
    spec, mod, path = load_module(name)
    if not path.exists():
        return -1, "", f"文件不存在: {path}"
    # 隔离 sys.argv，避免子模块里的 argparse 读到 pipeline 的参数
    saved_argv = sys.argv
    sys.argv = [f"{name}.py"]
    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(stdout_buf), contextlib.redirect_stderr(stderr_buf):
            spec.loader.exec_module(mod)
            if hasattr(mod, "main"):
                mod.main()
        return 0, stdout_buf.getvalue(), stderr_buf.getvalue()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
        return code, stdout_buf.getvalue(), stderr_buf.getvalue()
    except BaseException as e:
        import traceback
        return 1, stdout_buf.getvalue(), traceback.format_exc()
    finally:
        sys.argv = saved_argv


def main():
    # 解析参数
    args = sys.argv[1:]
    if "--period" in args:
        idx    = args.index("--period")
        period = args[idx + 1] if idx + 1 < len(args) else ""
    elif args and not args[0].startswith("-"):
        period = args[0].strip()
    else:
        period = ""

    only_steps: set[str] | None = None
    if "--only" in args:
        idx = args.index("--only")
        only_val = args[idx + 1] if idx + 1 < len(args) else ""
        only_sources = [s.strip() for s in only_val.split(",") if s.strip()]
        unknown = [s for s in only_sources if s not in SOURCE_STEPS]
        if unknown:
            print(f"❌ --only 中存在未知 source: {', '.join(unknown)}")
            print(f"   支持的 source: {', '.join(SOURCE_STEPS.keys())}")
            sys.exit(1)
        only_steps = set()
        for s in only_sources:
            only_steps.update(SOURCE_STEPS[s])

    if not period:
        period = input("请输入账期 (如 2512): ").strip()
    if not period:
        print("账期不能为空"); sys.exit(1)

    # 依赖检查
    check_dependencies()

    # 修改 config.py
    set_period(period)

    # 过滤流水线
    if only_steps is not None:
        pipeline = [s for s in PIPELINE if s in only_steps]
        print(f"  --only 模式：仅执行 {len(pipeline)} 个步骤 ({', '.join(pipeline)})")
    else:
        pipeline = list(PIPELINE)

    # 准备日志
    log_dir  = ROOT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    ts       = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"pipeline_{period}_{ts}.log"

    print(f"\n{'='*60}")
    print(f"  账本流水线  |  账期: {period}")
    print(f"  主目录: {ROOT_DIR}")
    print(f"  日志: logs/pipeline_{period}_{ts}.log")
    print(f"{'='*60}\n")

    failed = []
    skipped = set()
    t0     = datetime.datetime.now()

    with open(log_path, "w", encoding="utf-8") as log:
        log.write(f"账期: {period}\n开始: {t0}\n主目录: {ROOT_DIR}\n\n")

        for i, name in enumerate(pipeline, 1):
            prefix = f"[{i}/{len(pipeline)}]"
            path   = SCRIPT_DIR / f"{name}.py"

            if not path.exists():
                msg = f"{prefix} 跳过 (不存在): {name}.py"
                print(msg); log.write(msg + "\n\n")
                continue

            if name in skipped:
                msg = f"{prefix} 跳过 (上游失败): {name}"
                print(msg); log.write(msg + "\n\n")
                continue

            print(f"{prefix} > {name}", flush=True)
            log.write(f"{'-'*60}\n{prefix} > {name}\n{'-'*60}\n")

            s = datetime.datetime.now()
            os.chdir(ROOT_DIR)          # 确保 cwd 是主目录
            code, stdout, stderr = run_module(name)
            elapsed = (datetime.datetime.now() - s).total_seconds()

            if stdout: print(stdout, end="", flush=True); log.write(stdout)
            if stderr: print(stderr, end="", file=sys.stderr, flush=True); log.write("[STDERR]\n" + stderr)

            status = f"OK ({elapsed:.1f}s)" if code == 0 else f"FAIL exit={code} ({elapsed:.1f}s)"
            if code != 0:
                failed.append(name)
                downstream = SKIP_ON_FAILURE.get(name, [])
                for ds in downstream:
                    skipped.add(ds)
                if downstream:
                    print(f"  → 跳过下游: {', '.join(downstream)}")
                    log.write(f"  → 跳过下游: {', '.join(downstream)}\n")
            print(status + "\n"); log.write(status + "\n\n")

        total = (datetime.datetime.now() - t0).total_seconds()
        summary_lines = [
            "=" * 60,
            f"完成: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  耗时: {total:.1f}s",
            f"结果: {len(pipeline)-len(failed)-len(skipped)} 成功 / {len(skipped)} 跳过 / {len(failed)} 失败 / {len(pipeline)} 共",
        ]
        if failed: summary_lines.append(f"失败: {', '.join(failed)}")
        if skipped: summary_lines.append(f"跳过: {', '.join(skipped)}")
        summary_lines.append("=" * 60)
        summary = "\n".join(summary_lines)
        print("\n" + summary); log.write("\n" + summary + "\n")

    print(f"\n日志: {log_path}")

    unknowns = count_unknowns(period)
    if unknowns:
        print(f"\n{'='*60}")
        print("  ⚠️  未分类条目（可运行 reclassifier 细化）:")
        for acct, cnt in sorted(unknowns.items()):
            print(f"    {acct}: {cnt} 条")
        print(f"{'='*60}")

    # 收支方向守卫：揪出「收入误挂支出腿」（收入被记成负支出，污染支出报表）。
    # 银行卡的 balance 断言查不出这类错误，故每次 pipeline 末尾扫一遍（仅告警不阻断）。
    try:
        import check_direction
        dir_problems = check_direction.scan()
        if dir_problems:
            tot = sum(p["amount"] for p in dir_problems)
            print(f"\n{'='*60}")
            print(f"  ⚠️  方向待复核：{len(dir_problems)} 笔「收入挂支出腿」共 ¥{tot:.0f}"
                  f"（详见 python 脚本/check_direction.py）")
            print(f"{'='*60}")
    except Exception as e:
        print(f"  [方向守卫跳过] {e}")

    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
