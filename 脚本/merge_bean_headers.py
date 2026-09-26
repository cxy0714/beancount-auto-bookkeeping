#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合并交易首行折行
================
原始流水里 payee / narration 字符串内部偶尔带有换行符，bean_edit_* 生成时会让
交易首行（形如 `2023-04-21 * "payee" "narration"`）断成两行甚至多行，例如：

    2023-04-21 * "示例支付-示例商户
    名称" "示例支付-示例商户
    名称"

本步骤在所有 bean 生成完成后运行，把这类被换行打断的首行直接拼接回单行
（换行处直接相连、不加空格），其余元数据行（payee_raw / goods 等）保持原样：

    2023-04-21 * "示例支付-示例商户名称" "示例支付-示例商户名称"

判定方式：交易首行以「日期 + 标记(*/!)」开头；若该行内引号数为奇数，说明字符串
被换行截断，继续向下拼接物理行，直到引号闭合（总数为偶数）为止。
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from config import PERIOD

ROOT_DIR = Path(__file__).parent.parent

_HEADER_RE = re.compile(r'^\d{4}-\d{2}-\d{2}\s+[*!]\s')


def merge_headers(text: str) -> tuple[str, int]:
    """合并 text 中被换行打断的交易首行，返回 (新文本, 合并条数)。"""
    lines = text.split('\n')
    out, i, n, joins = [], 0, len(lines), 0
    while i < n:
        line = lines[i]
        if _HEADER_RE.match(line) and line.count('"') % 2 != 0:
            merged = line
            i += 1
            # 引号未闭合 → 继续吞并后续物理行（换行处直接相连）
            while i < n and merged.count('"') % 2 != 0:
                merged += lines[i]
                i += 1
            out.append(merged)
            joins += 1
        else:
            out.append(line)
            i += 1
    return '\n'.join(out), joins


def main():
    bean_dir = ROOT_DIR / "bean_files" / PERIOD
    if not bean_dir.exists():
        print(f"  跳过：bean 目录不存在 {bean_dir}")
        return

    total_files, total_joins = 0, 0
    for f in sorted(bean_dir.glob("*.bean")):
        if f.name.endswith(".bak"):
            continue
        raw = f.read_text(encoding="utf-8")
        new, joins = merge_headers(raw)
        if joins and new != raw:
            f.write_text(new, encoding="utf-8")
            total_files += 1
            total_joins += joins
            print(f"  {f.name}: 合并 {joins} 处折行首行")

    if total_joins:
        print(f"  合计：{total_files} 个文件，{total_joins} 处首行已合并为单行")
    else:
        print("  无折行首行，无需处理")


if __name__ == "__main__":
    main()
