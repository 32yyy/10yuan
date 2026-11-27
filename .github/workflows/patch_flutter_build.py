#!/usr/bin/env python3
"""
patch_flutter_build.py

用法：
    python3 .github/workflows/patch_flutter_build.py

作用：
    1. 给 .github/workflows/flutter-build.yml 的 on.workflow_call 补 secrets 声明；
    2. 给顶层 env 补自定义环境变量；
    3. 给产物文件名加 CUSTOM_SUFFIX 后缀；
    4. 注释 .github/workflows/flutter-nightly.yml 的 schedule，避免夜间构建。

设计原则：
    - 幂等：重复运行不会重复插入；
    - 精准：只做文本插入/替换，不重排整个 YAML，保留注释与缩进；
    - 解耦：secrets、env、文件名三件事分别判断，互不干扰。
"""

import re
import sys
from pathlib import Path

FLUTTER_BUILD = Path(".github/workflows/flutter-build.yml")
FLUTTER_NIGHTLY = Path(".github/workflows/flutter-nightly.yml")

CUSTOM_SECRETS = [
    "RENDEZVOUS_SERVER",
    "RS_PUB_KEY",
    "API_SERVER",
    "DEFAULT_PASSWORD",
    "RELAY_SERVER",
    "CUSTOM_SUFFIX",
    "SOS_MODE",
]

CUSTOM_ENV = {
    "RS_PUB_KEY": '"${{ secrets.RS_PUB_KEY }}"',
    "RENDEZVOUS_SERVER": '"${{ secrets.RENDEZVOUS_SERVER }}"',
    "API_SERVER": '"${{ secrets.API_SERVER }}"',
    "DEFAULT_PASSWORD": '"${{ secrets.DEFAULT_PASSWORD }}"',
    "CUSTOM_SUFFIX": '"${{ secrets.CUSTOM_SUFFIX }}"',
    "SOS_MODE": "${{ secrets.SOS_MODE }}",
}

SECRETS_MARKER = "# ===== 新增的自定义 secrets ====="
ENV_MARKER = "# ===== 新增的自定义环境变量 ====="


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def find_block_range(content: str, header_regex: str):
    """
    给定一个块头正则（如 r'^on:\s*\n(?:.*\n)*?^  workflow_call:\s*\n'），
    返回该块体在 content 中的 [start, end) 区间。

    end 的判定：从 start 开始按行扫描，遇到第一个“缩进 <= 块头缩进”的非空行，
    即为块结束；否则到文件尾。
    """
    m = re.search(header_regex, content, re.MULTILINE)
    if not m:
        return None
    start = m.end()

    # 推断块头的缩进：取 header_regex 匹配到的最后一行（即 workflow_call: 那行）
    header_lines = m.group(0).splitlines()
    if not header_lines:
        return None
    header_line = header_lines[-1]
    header_indent = len(header_line) - len(header_line.lstrip())

    offset = start
    for line in content[start:].splitlines(keepends=True):
        if line.strip() == "":
            offset += len(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent <= header_indent:
            return (start, offset)
        offset += len(line)
    return (start, len(content))


def insert_after_last_child(content: str, block_range, lines_to_insert, child_indent):
    """
    把 lines_to_insert 插到 block_range 内最后一个子行之后（保持块内顺序自然）。
    lines_to_insert 是已经带好缩进的字符串列表（每项含换行）。
    """
    start, end = block_range
    block = content[start:end]

    # 去掉块尾多余空行，找到最后一个非空子行的结束位置
    stripped = block.rstrip("\n")
    trailing_newlines = block[len(stripped):]
    insert_at = start + len(stripped)
    if not stripped.endswith("\n"):
        insert_at += 1
        prefix = "\n"
    else:
        prefix = ""

    inserted = prefix + "".join(lines_to_insert)
    # 保持原有的尾部换行数量（至少 1 个）
    tail = trailing_newlines if trailing_newlines else "\n"
    return content[:insert_at] + inserted + tail + content[end:]


# ---------------------------------------------------------------------------
# 1) 补 on.workflow_call.secrets
# ---------------------------------------------------------------------------

def ensure_workflow_call_secrets(content: str) -> str:
    block_range = find_block_range(content, r'^on:\s*\n(?:.*\n)*?^  workflow_call:\s*\n')
    if not block_range:
        print("⚠️ 未找到 on.workflow_call，跳过 secrets 插入")
        return content

    start, end = block_range
    block = content[start:end]

    # 已有 secrets: 行？
    if re.search(r'^    secrets:\s*$', block, re.MULTILINE):
        # 找出已声明的 secret 名
        existing = set(re.findall(r'^      ([A-Z][A-Z0-9_]+):\s*$', block, re.MULTILINE))
        missing = [s for s in CUSTOM_SECRETS if s not in existing]
        if not missing:
            print("ℹ️ workflow_call.secrets 已包含全部自定义 secret")
            return content

        # 在 secrets: 块内补缺失项
        sec_match = re.search(r'^    secrets:\s*$', block, re.MULTILINE)
        sec_start = start + sec_match.end()
        # 找 secrets 块的结束：下一个缩进 <= 4 的非空行
        offset = sec_start
        for line in content[sec_start:end].splitlines(keepends=True):
            if line.strip() == "":
                offset += len(line)
                continue
            indent = len(line) - len(line.lstrip())
            if indent <= 4:
                break
            offset += len(line)

        lines = []
        if SECRETS_MARKER not in block:
            lines.append(f"      {SECRETS_MARKER}\n")
        for s in missing:
            lines.append(f"      {s}:\n        required: false\n")
        content = content[:offset] + "".join(lines) + content[offset:]
        print(f"✅ workflow_call.secrets 补齐缺失项: {', '.join(missing)}")
        return content

    # 没有 secrets:，在 workflow_call 块末尾创建
    lines = ["    secrets:\n", f"      {SECRETS_MARKER}\n"]
    for s in CUSTOM_SECRETS:
        lines.append(f"      {s}:\n        required: false\n")
    return insert_after_last_child(content, block_range, lines, "      ")


# ---------------------------------------------------------------------------
# 2) 补顶层 env
# ---------------------------------------------------------------------------

def ensure_env(content: str) -> str:
    # 找顶层 env 块（缩进 0 的 env:）
    m = re.search(r'^env:\s*\n', content, re.MULTILINE)
    if not m:
        print("⚠️ 未找到顶层 env: 块，跳过 env 插入")
        return content

    start = m.end()
    offset = start
    for line in content[start:].splitlines(keepends=True):
        if line.strip() == "":
            offset += len(line)
            continue
        indent = len(line) - len(line.lstrip())
        if indent == 0:
            break
        offset += len(line)

    block_range = (start, offset)
    block = content[start:offset]

    existing_keys = set(re.findall(r'^  ([A-Z][A-Z0-9_]+):', block, re.MULTILINE))
    missing = {k: v for k, v in CUSTOM_ENV.items() if k not in existing_keys}
    if not missing:
        print("ℹ️ 顶层 env 已包含全部自定义环境变量")
        return content

    lines = []
    if ENV_MARKER not in block:
        lines.append(f"  {ENV_MARKER}\n")
    for k, v in missing.items():
        lines.append(f"  {k}: {v}\n")
    content = insert_after_last_child(content, block_range, lines, "  ")
    print(f"✅ 顶层 env 补齐缺失项: {', '.join(missing.keys())}")
    return content


# ---------------------------------------------------------------------------
# 3) 文件名加 CUSTOM_SUFFIX
# ---------------------------------------------------------------------------

SUFFIX_EXPR = r"${{ env.CUSTOM_SUFFIX != '' && format('-{0}', env.CUSTOM_SUFFIX) || '' }}"


def patch_filenames(content: str) -> str:
    # 已经是打过补丁的文件名就跳过
    if SUFFIX_EXPR in content:
        print("ℹ️ 文件名后缀已存在，跳过")
        return content

    replacements = [
        # exe
        (
            r'mv ./target/release/rustdesk-portable-packer\.exe ./SignOutput/rustdesk-\$\{\{ env\.VERSION \}\}-\$\{\{ matrix\.job\.arch \}\}\.exe',
            f'mv ./target/release/rustdesk-portable-packer.exe ./SignOutput/rustdesk-${{{{ env.VERSION }}}}-${{{{ matrix.job.arch }}}}{SUFFIX_EXPR}.exe',
        ),
        # msi
        (
            r'(rustdesk-\$\{\{ env\.VERSION \}\}-\$\{\{ matrix\.job\.arch \}\})\.msi',
            rf'\1{SUFFIX_EXPR}.msi',
        ),
        # -sciter.exe
        (
            r'(rustdesk-\$\{\{ env\.VERSION \}\}-\$\{\{ matrix\.job\.arch \}\})-sciter\.exe',
            rf'\1{SUFFIX_EXPR}-sciter.exe',
        ),
    ]

    for pattern, repl in replacements:
        new_content = re.sub(pattern, repl, content)
        if new_content != content:
            print(f"✅ 文件名后缀已应用: {pattern}")
            content = new_content
        else:
            print(f"⚠️ 未匹配到文件名模式: {pattern}")

    return content


# ---------------------------------------------------------------------------
# 4) 处理 flutter-nightly.yml 的 schedule
# ---------------------------------------------------------------------------

def patch_nightly_schedule():
    if not FLUTTER_NIGHTLY.exists():
        print("⚠️ flutter-nightly.yml 不存在，跳过")
        return
    content = FLUTTER_NIGHTLY.read_text()
    if "  # schedule:" in content:
        print("ℹ️ flutter-nightly.yml 的 schedule 已注释")
        return
    new_content = content.replace("  schedule:", "  # schedule:")
    new_content = new_content.replace("    - cron:", "    # - cron:")
    if new_content == content:
        print("⚠️ flutter-nightly.yml 未匹配到 schedule，跳过")
        return
    FLUTTER_NIGHTLY.write_text(new_content)
    print("✅ flutter-nightly.yml 的 schedule 已注释")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    if not FLUTTER_BUILD.exists():
        print(f"❌ 文件不存在: {FLUTTER_BUILD}")
        sys.exit(1)

    content = FLUTTER_BUILD.read_text()

    content = ensure_workflow_call_secrets(content)
    content = ensure_env(content)
    content = patch_filenames(content)

    FLUTTER_BUILD.write_text(content)
    print("✅ flutter-build.yml 自定义完成")

    patch_nightly_schedule()
    print("✅ 所有补丁完成")


if __name__ == "__main__":
    main()