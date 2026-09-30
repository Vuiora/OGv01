"""对 M12 做定向变异测试：故意破坏沙箱的关键防线，确认测试能抓到。

做法：把 ``sdl_m12/sandbox.py`` 备份到内存 → 逐条注入变异 → 跑
``tests.test_sandbox`` → 记录是否失败 → 还原。全部变异都应被捕获，
否则说明断言是空洞的。

运行：``python scripts/mutate_m12.py``
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "sdl_m12" / "sandbox.py"
BACKUP = ROOT / "sdl_m12" / ".sandbox.py.mutation-backup"
PY = sys.executable

#: (变异名, 原始片段, 替换片段)。每条都应使测试失败（返回码非 0）。
MUTATIONS: list[tuple[str, str, str]] = [
    # 1. 让「检查不过也执行」——直接废掉最关键的不变式
    (
        "放宽：检查不过仍执行",
        "    if not verdict.allowed:\n"
        "        # 硬约束：检查不过就不执行。这里直接返回，不进入编译/执行分支。\n"
        "        return SandboxRun(status=STATUS_REJECTED, verdict=verdict)\n",
        "    if False:\n"
        "        return SandboxRun(status=STATUS_REJECTED, verdict=verdict)\n",
    ),
    # 2. 放过非白名单导入
    (
        "放宽：导入不查白名单",
        "                if top not in ALLOWED_MODULES:\n"
        "                    _add(\"禁止模块与名字\", f\"不允许导入 {alias.name}\",\n"
        "                         getattr(node, \"lineno\", 0))\n",
        "                pass\n",
    ),
    # 3. 禁用名不再拦截
    (
        "放宽：禁用名不拦",
        "            if node.id in FORBIDDEN_NAMES and node.id not in local_names:\n",
        "            if False and node.id in FORBIDDEN_NAMES and node.id not in local_names:\n",
    ),
    # 4. 下划线属性访问不拦
    (
        "放宽：下划线属性不拦",
        "            if node.attr.startswith(\"_\"):\n",
        "            if False and node.attr.startswith(\"_\"):\n",
    ),
    # 5. 边界常量说谎（自检却应抓到）
    (
        "破坏：边界常量翻转",
        "SANDBOX_RUNS_UNCHECKED_CODE = False",
        "SANDBOX_RUNS_UNCHECKED_CODE = True",
    ),
    # 6. 移除超时检查点
    (
        "放宽：去掉超时判定",
        "        if (counter[\"n\"] & 0x3FF) == 0 and time.monotonic() > deadline:\n"
        "            raise _BudgetExceeded(\"执行超时\")\n",
        "        pass\n",
    ),
]


def _run_tests() -> int:
    # 变异「去掉超时判定」会让墙钟用例真的死循环——必须给子进程加硬超时，
    # 否则变异脚本本身会挂住。超时=捕获（返回码非 0）。
    try:
        proc = subprocess.run(
            [PY, "-m", "unittest", "tests.test_sandbox"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return 124  # 与非零同义：变异被捕获
    return proc.returncode


def main() -> int:
    # 先把原文落到**磁盘备份**：万一进程被强杀（挂死的变异会让子进程永不返回），
    # 下次运行能凭备份自动还原，不会把变异残留在源码里（此前踩过一坑）。
    if BACKUP.exists() and not (TARGET.read_text(encoding="utf-8") ==
                                BACKUP.read_text(encoding="utf-8")):
        print(f"[还原] 检测到上次异常中断的残留变异，先从 {BACKUP.name} 还原。")
        TARGET.write_text(BACKUP.read_text(encoding="utf-8"), encoding="utf-8")
    original = TARGET.read_text(encoding="utf-8")
    BACKUP.write_text(original, encoding="utf-8")

    baseline = _run_tests()
    print(f"[基线] tests.test_sandbox 返回码 = {baseline}（应为 0）")
    if baseline != 0:
        print("基线就不通过，先修好再变异。")
        TARGET.write_text(original, encoding="utf-8")
        return 1

    caught, missed = [], []
    try:
        for name, old, new in MUTATIONS:
            if old not in original:
                print(f"[跳过] {name}：源码未找到锚点片段")
                missed.append(name)
                continue
            TARGET.write_text(original.replace(old, new, 1), encoding="utf-8")
            try:
                rc = _run_tests()
            except BaseException:
                # 连本进程都被打断：立刻还原再抛出。
                TARGET.write_text(original, encoding="utf-8")
                raise
            finally:
                TARGET.write_text(original, encoding="utf-8")
            if rc != 0:
                caught.append(name)
                print(f"[捕获] {name} → 测试失败（符合预期）")
            else:
                missed.append(name)
                print(f"[漏网] {name} → 测试仍通过（断言空洞！）")
    finally:
        TARGET.write_text(original, encoding="utf-8")
        if BACKUP.exists():
            BACKUP.unlink()

    print(f"\n捕获 {len(caught)}/{len(MUTATIONS)}，漏网 {len(missed)}")
    if missed:
        print("漏网清单：", missed)
        return 1
    print("全部变异均被捕获——断言非空洞。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
