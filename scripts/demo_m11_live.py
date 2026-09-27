"""M11 真实 LLM 端到端演示：让真 endpoint 驱动 M10 工具箱做一次发现分析。

与 ``tests/test_driver.py`` 的离线假 client 不同，本脚本用**真实 HTTP endpoint**
（凭据只从 ``SDL_LLM_*`` 环境变量读取，绝不落盘）。

链路：真实 M1 金库（合成数据）→ 注入 explorer → M10 工具目录 →
:func:`sdl_m11.driver.run_driver` 把目录交给 LLM → LLM 自主决定调用哪些算法 →
M11 经 ``Toolbox.call()`` 执行 → 结果回喂 → 停止。

用法::

    export SDL_LLM_BASE_URL=https://llm.ujn.edu.cn/v1
    export SDL_LLM_API_KEY=...
    export SDL_LLM_MODEL=Qwen3.8-Flash-Next
    python scripts/demo_m11_live.py [输出目录]

产物：``live-driver.json``（完整会话轨迹）、``live-driver.md``（人读摘要）。
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sdl_m01 import Module01, initialize                       # noqa: E402
from sdl_m10.toolbox import build_default_toolbox              # noqa: E402
from sdl_m11.driver import (                                   # noqa: E402
    DriverLimits, EndpointConfig, HttpLLMClient, build_system_prompt,
    run_driver, tool_schema,
)
from sdl_pipeline.loop import synthetic_records, synthetic_spec  # noqa: E402

OUT_DIR = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "demo-output" / "p19-live"

TASK = (
    "你现在面对一份探索数据（特征变量 X1、X2，目标 Y）。请自主使用可用工具完成一次"
    "统计分析：\n"
    "1) 先提出 1–2 个「关于 Y 如何由这些变量构成」的候选结构（表达式）。\n"
    "2) 用工具在探索分区上制备样本并拟合你的候选，得到拟合优度。\n"
    "3) 建立对照基线，比较你的候选是否真的优于基线。\n"
    "4) 给出一句话结论，并明确说明你的分析处于什么证据地位。\n"
    "注意：只能引用工具返回的句柄（h_ 开头）来衔接步骤。可用的变量名只有 X1、X2；"
    "Y 是目标，不作为自变量。"
)


def _make_vault() -> tuple[Module01, str]:
    """建真实 M1 金库，返回 (explorer, protocol_id)。"""
    directory = tempfile.mkdtemp(prefix="sdl-m11-live-")
    tokens = initialize(os.path.join(directory, "evidence.sqlite3"))
    custodian = Module01(os.path.join(directory, "evidence.sqlite3"), tokens["custodian"])
    explorer = Module01(os.path.join(directory, "evidence.sqlite3"), tokens["explorer"])
    protocol = custodian.build(synthetic_spec(), synthetic_records())
    return explorer, protocol["protocol_id"]


def main() -> int:
    try:
        cfg = EndpointConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] endpoint 未配置：{exc}")
        print("      需要 SDL_LLM_BASE_URL / SDL_LLM_MODEL（密钥置于 SDL_LLM_API_KEY）。")
        return 2

    print("=" * 72)
    print("M11 真实 LLM 端到端演示")
    print("=" * 72)
    print(f"endpoint : {cfg.base_url}")
    print(f"model    : {cfg.model}")
    print(f"api_key  : {cfg.redacted()['has_api_key']}（已提供，明文不出现在任何输出中）")
    print(f"配置指纹 : {cfg.content_digest()}（不含密钥）")
    print()

    explorer, protocol_id = _make_vault()
    toolbox = build_default_toolbox(explorer=explorer, seed=7)

    cat = toolbox.catalogue()
    tools = tool_schema(toolbox)
    print(f"工具目录 : {len(tools)} 个工具")
    for t in cat["tools"]:
        print(f"  - {t['name']:<26} {t['summary'][:44]}")
    print()

    client = HttpLLMClient(cfg)
    limits = DriverLimits(max_rounds=12, max_tool_calls=30)

    print("—— 开始驱动（LLM 自主决定调用顺序与参数）——")
    started = time.monotonic()
    session = run_driver(
        client=client,
        toolbox=toolbox,
        limits=limits,
        user_task=TASK + f"\n（protocol_id = {protocol_id}）",
        extra_system=(
            "本轮可用工具的参数名以工具 schema 为准。制备样本需要 protocol_id、"
            "variables、target；拟合需要 sample 句柄与 relationship（表达式句柄）。"
        ),
    )
    elapsed = time.monotonic() - started

    print()
    print("=" * 72)
    print(f"会话结束：stop_reason={session.stop_reason}  "
          f"工具调用={session.tool_call_count}  轮次={len(session.steps)}  耗时={elapsed:.1f}s")
    print("=" * 72)
    for step in session.steps:
        print(f"\n[第 {step.round_index} 轮] 助手文本：{(step.assistant_text or '')[:120]}")
        for call, res in zip(step.tool_calls, step.results):
            args = json.dumps(dict(call.arguments), ensure_ascii=False)
            print(f"  → {call.name}({args[:90]})")
            print(f"    ← status={res.status} handle={res.handle} reason={res.reason}")
            summary = json.dumps(res.summary, ensure_ascii=False, default=str)
            print(f"      {summary[:160]}")

    print("\n最终陈述：")
    print("  " + (session.final_text or "(无)").replace("\n", "\n  "))

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "live-driver.json").write_text(
        json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# M11 真实 LLM 端到端演示",
        "",
        f"- endpoint: `{cfg.base_url}`",
        f"- model: `{cfg.model}`",
        f"- 配置指纹（不含密钥）: `{cfg.content_digest()}`",
        f"- stop_reason: `{session.stop_reason}`",
        f"- 工具调用数: {session.tool_call_count}，轮次: {len(session.steps)}，耗时: {elapsed:.1f}s",
        f"- 会话内容指纹: `{session.content_digest()[:16]}`",
        "",
        "> 本演示使用**合成数据**，仅验证「LLM 驱动算法工具」这条链路，"
        "不宣称任何现实理论或因果结论。证据地位由系统确证机制判定，LLM 无权授予。",
        "",
        "## 工具调用轨迹",
        "",
    ]
    for step in session.steps:
        md.append(f"### 第 {step.round_index} 轮")
        if step.assistant_text:
            md.append(f"\n> {step.assistant_text[:300]}\n")
        for call, res in zip(step.tool_calls, step.results):
            md.append(f"- `{call.name}({json.dumps(dict(call.arguments), ensure_ascii=False)})`")
            md.append(f"  - status=`{res.status}` handle=`{res.handle}` reason=`{res.reason}`")
            md.append(f"  - {json.dumps(res.summary, ensure_ascii=False, default=str)[:300]}")
    md += ["", "## 最终陈述", "", session.final_text or "(无)", ""]
    (OUT_DIR / "live-driver.md").write_text("\n".join(md), encoding="utf-8")

    print(f"\n产物已写入：{OUT_DIR}/live-driver.json, live-driver.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
