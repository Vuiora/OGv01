"""对 M11 做定向变异测试：故意破坏驱动层的关键防线，确认测试能抓到。

与 ``scripts/mutate_m12.py`` 同构：备份 → 逐条注入变异 → 跑 ``tests.test_driver``
→ 记录是否失败 → 还原。全部变异都应被捕获，否则断言是空洞的。

运行：``python scripts/mutate_m11.py``
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
TARGET = ROOT / "sdl_m11" / "driver.py"
BACKUP = ROOT / "sdl_m11" / ".driver.py.mutation-backup"
PY = sys.executable

MUTATIONS: list[tuple[str, str, str]] = [
    # 1. 绕过 M10：工具调用直接返回成功桩，不经 Toolbox.call
    (
        "破坏：绕过 M10 护栏",
        "    # 其余工具：一律经 M10 的 call()，受四类护栏与封存泄漏检查约束。\n"
        "    return toolbox.call(call.name, **dict(call.arguments))",
        "    return ToolResult(call.name, STATUS_OK, None, {})",
    ),
    # 2. 密钥泄漏进 canonical_json（凭据纪律）
    (
        "破坏：密钥入规范化形式",
        "    def canonical_json(self) -> str:\n"
        "        return json.dumps(self.redacted(), ensure_ascii=False, sort_keys=True,\n"
        "                          separators=(\",\", \":\"))",
        "    def canonical_json(self) -> str:\n"
        "        d = self.redacted()\n"
        "        d[\"api_key\"] = self.api_key\n"
        "        return json.dumps(d, ensure_ascii=False, sort_keys=True,\n"
        "                          separators=(\",\", \":\"))",
    ),
    # 3. to_dict 不掩码
    (
        "破坏：密钥不掩码",
        "        d[\"api_key\"] = \"***\" if self.api_key else \"\"",
        "        d[\"api_key\"] = self.api_key",
    ),
    # 4. 无工具调用时不停（应继续循环）——使 no_tool_call 停止逻辑失效
    (
        "放宽：忽略 no_tool_call",
        "        if not reply.tool_calls:\n",
        "        if False and not reply.tool_calls:\n",
    ),
    # 5. 预算失守：max_tool_calls 判定永假
    (
        "放宽：预算不生效",
        "            if calls_used >= limits.max_tool_calls:\n",
        "            if False and calls_used >= limits.max_tool_calls:\n",
    ),
    # 6. LLM 报错不再降级（应抛出去）
    (
        "破坏：endpoint 错误不降级",
        "            reply = client.complete(messages, tools)",
        "            reply = client.complete(messages, tools)\n"
        "            raise RuntimeError('no-degrade') if getattr(reply, 'text', None) else None",
    ),
    # 7. 默认就暴露沙箱工具（安全默认被破坏）
    (
        "破坏：默认开启代码提交",
        "    allow_code_submission: bool = False,",
        "    allow_code_submission: bool = True,",
    ),
    # 8. 边界常量说谎
    (
        "破坏：边界常量翻转",
        "DRIVER_BYPASSES_TOOLBOX = False",
        "DRIVER_BYPASSES_TOOLBOX = True",
    ),
]


def _run_tests() -> int:
    try:
        proc = subprocess.run(
            [PY, "-m", "unittest", "tests.test_driver"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired:
        return 124
    return proc.returncode


def main() -> int:
    # 磁盘备份：进程被强杀时下次运行可凭它还原（防止变异残留）。
    if BACKUP.exists() and not (TARGET.read_text(encoding="utf-8") ==
                                BACKUP.read_text(encoding="utf-8")):
        print(f"[还原] 检测到上次异常中断的残留变异，先从 {BACKUP.name} 还原。")
        TARGET.write_text(BACKUP.read_text(encoding="utf-8"), encoding="utf-8")
    original = TARGET.read_text(encoding="utf-8")
    BACKUP.write_text(original, encoding="utf-8")

    baseline = _run_tests()
    print(f"[基线] tests.test_driver 返回码 = {baseline}（应为 0）")
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
