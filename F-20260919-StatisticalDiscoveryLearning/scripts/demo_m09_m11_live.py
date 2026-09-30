"""真实 LLM 驱动「取数 + 分析」端到端实测（M9 × M11 联调）。

与 ``scripts/demo_m11_live.py`` 的区别：那个只跑 M11（LLM 分析一个**已有**的金库）；
本脚本多跑一个**真实的自主取数阶段**（M9 的端到端入口
:func:`sdl_m09.collection.run_autonomous_discovery`），把「取回来的数据」也变成产物，
再让真实 endpoint 驱动 LLM 在**同一数据生成过程**上用算法工具做分析。

两阶段与证据隔离设计的关系（务必先读懂再解读产物）：

- **阶段一（取数）**：M7 出取证建议 → M9 按建议真取数 → 经 M1 登记进**确证分区 C**。
  取回的批次数、原因码、规律清单都是**机械产出**，由 M8 判定等级。
- **阶段二（分析）**：M11 把 M10 工具目录交给 LLM，LLM 在**探索分区 E / 开发评估分区 V**
  上自主调用算法。它**看不到**确证分区 C 的内容——这正是隔离设计的目的。

因此本脚本**不会**把两阶段的结果混为一谈：阶段一的规律有证据等级，阶段二的陈述
**永远只是探索性结论**（LLM 无权授予等级）。脚本把阶段一的取数轨迹作为**背景上下文**
喂给 LLM，使它的分析有所依据、并如实说明自身证据地位。

凭据纪律：只从 ``SDL_LLM_BASE_URL`` / ``SDL_LLM_API_KEY`` / ``SDL_LLM_MODEL`` /
``SDL_LLM_TIMEOUT`` 读取；密钥绝不落盘、不进提示词、不出现在任何产物中。

用法::

    export SDL_LLM_BASE_URL=https://llm.ujn.edu.cn/v1
    export SDL_LLM_API_KEY=...
    export SDL_LLM_MODEL=Qwen3.8-Flash-Next
    python scripts/demo_m09_m11_live.py [输出目录] [--groups 24] [--max-rounds 10]

产物：``acquisition/``（阶段一）、``analysis/``（阶段二）、``报告.md``（合并报告）。
"""

from __future__ import annotations

import argparse
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
from sdl_m09.collection import (                                # noqa: E402
    run_autonomous_discovery,
)
from sdl_m10.toolbox import build_default_toolbox              # noqa: E402
from sdl_m11.driver import (                                   # noqa: E402
    DriverLimits, EndpointConfig, HttpLLMClient, run_driver, tool_schema,
)
from sdl_pipeline.loop import synthetic_records, synthetic_spec  # noqa: E402


# ---------------------------------------------------------------------------
# 阶段一：真实自主取数
# ---------------------------------------------------------------------------


class RecordingSource:
    """包一层数据源，把**真实取回来的**记录留档（供报告引用，不改变取数行为）。"""

    def __init__(self, inner):
        self.inner = inner
        self.batches: list[list[dict]] = []
        self.notice = getattr(inner, "notice", None)

    def __call__(self, request, round_index):
        records = list(self.inner(request, round_index))
        self.batches.append(records)
        return records


def run_acquisition(out_dir: pathlib.Path, groups: int) -> dict:
    """阶段一：跑真实的自主取数端到端入口，落盘并返回载荷。"""
    from sdl_m09.collection import SyntheticSource

    source = RecordingSource(SyntheticSource())
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    payload = run_autonomous_discovery(str(out_dir), groups=groups, source=source)
    elapsed = time.monotonic() - started

    acquired = [r for batch in source.batches for r in batch]
    return {
        "payload": payload,
        "elapsed": elapsed,
        "acquired_record_count": len(acquired),
        "acquired_batch_count": len(source.batches),
        "sample_records": acquired[:4],
    }


# ---------------------------------------------------------------------------
# 阶段二：真实 LLM 驱动分析
# ---------------------------------------------------------------------------


def _make_vault(seed: int) -> tuple[Module01, str]:
    """建一个 LLM 可读（explorer 角色）的 M1 金库，返回 (explorer, protocol_id)。"""
    directory = tempfile.mkdtemp(prefix="sdl-m09m11-live-")
    db = os.path.join(directory, "evidence.sqlite3")
    tokens = initialize(db)
    custodian = Module01(db, tokens["custodian"])
    explorer = Module01(db, tokens["explorer"])
    protocol = custodian.build(synthetic_spec(seed), synthetic_records())
    return explorer, protocol["protocol_id"]


def _acquisition_briefing(acq: dict) -> str:
    """把阶段一的取数轨迹压缩成一段背景上下文（喂给 LLM，不含确证分区内容）。"""
    payload = acq["payload"]
    reg = payload["regularities"]
    coll = payload["collection"]
    lines = [
        "【背景：本轮已完成一次自主取数（供你参考取数结果，但你不应据此直接下结论）】",
        f"- 取数轮次：{coll.get('round_count')}，已用批次预算：{coll.get('batches_used')}",
        f"- 真实取回批次数：{acq['acquired_batch_count']}，记录数：{acq['acquired_record_count']}",
        f"- 机械判定的规律：支持 {reg.get('supported_count')} 条 / "
        f"反驳 {reg.get('refuted_count')} 条（知识库版本 {reg.get('knowledge_version')}）",
        "注意：上述规律由系统确证机制判定等级，**你无权引用或改写其等级**；"
        "你的任务是在探索分区上独立地用工具复现一次分析。",
    ]
    return "\n".join(lines)


def run_analysis(
    out_dir: pathlib.Path,
    acq: dict,
    *,
    seed: int,
    max_rounds: int,
    max_tool_calls: int,
) -> dict:
    """阶段二：用真实 endpoint 驱动 LLM 在探索分区上做一次分析。"""
    cfg = EndpointConfig.from_env()
    explorer, protocol_id = _make_vault(seed)
    toolbox = build_default_toolbox(explorer=explorer, seed=seed)
    tools = tool_schema(toolbox)

    task = (
        "你现在面对一份探索数据（特征变量 X1、X2，目标 Y）。请自主使用可用工具完成一次"
        "统计分析：\n"
        "1) 先提出 1–2 个「关于 Y 如何由这些变量构成」的候选结构（表达式）。\n"
        "2) 在探索分区上制备样本并拟合你的候选，得到拟合优度。\n"
        "3) 建立对照基线，比较你的候选是否真的优于基线。\n"
        "4) 给出结论，并**明确说明你的分析处于什么证据地位**。\n"
        "只能引用工具返回的句柄（h_ 开头）来衔接步骤。可用变量名只有 X1、X2；"
        "Y 是目标，不作为自变量。\n\n"
        + _acquisition_briefing(acq)
        + f"\n（protocol_id = {protocol_id}）"
    )

    client = HttpLLMClient(cfg)
    limits = DriverLimits(max_rounds=max_rounds, max_tool_calls=max_tool_calls)

    started = time.monotonic()
    session = run_driver(
        client=client,
        toolbox=toolbox,
        limits=limits,
        user_task=task,
        extra_system=(
            "本轮可用工具的参数名以工具 schema 为准。制备样本需要 protocol_id、"
            "variables、target；拟合需要 sample 句柄与 relationship（表达式句柄）。"
        ),
    )
    elapsed = time.monotonic() - started

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "live-driver.json").write_text(
        json.dumps(session.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "cfg": cfg,
        "tool_count": len(tools),
        "protocol_id": protocol_id,
        "session": session,
        "elapsed": elapsed,
    }


# ---------------------------------------------------------------------------
# 合并报告
# ---------------------------------------------------------------------------


def write_report(out_dir: pathlib.Path, acq: dict, ana: dict) -> None:
    payload = acq["payload"]
    reg = payload["regularities"]
    coll = payload["collection"]
    session = ana["session"]

    lines = [
        "# 真实 LLM 驱动「取数 + 分析」实测报告（M9 × M11）",
        "",
        "本报告由 `scripts/demo_m09_m11_live.py` 生成，使用**真实 LLM endpoint** 与"
        "**真实自主取数**，数据由代码合成（不宣称任何现实含义）。",
        "",
        "## 阶段一 · 自主取数（机械执行）",
        "",
        f"- 取数轮次：{coll.get('round_count')}，已用批次预算：{coll.get('batches_used')}",
        f"- 真实取回批次数：{acq['acquired_batch_count']}，记录数：{acq['acquired_record_count']}",
        f"- 耗时：{acq['elapsed']:.1f}s",
        f"- 规律清单（知识库 {reg.get('knowledge_version')}）："
        f"支持 {reg.get('supported_count')} / 反驳 {reg.get('refuted_count')} "
        f"（本轮新增 {reg.get('new_supported_count')}）",
        "",
        "> 阶段一的等级由 M8 **机械判定**，与 LLM 的文本无关。",
        "",
        "## 阶段二 · 真实 LLM 驱动分析（探索性）",
        "",
        f"- endpoint：`{ana['cfg'].base_url}`",
        f"- model：`{ana['cfg'].model}`",
        f"- 配置指纹（不含密钥）：`{ana['cfg'].content_digest()}`",
        f"- 工具目录：{ana['tool_count']} 个工具",
        f"- stop_reason：`{session.stop_reason}`，工具调用 {session.tool_call_count} 次，"
        f"轮次 {len(session.steps)}，耗时 {ana['elapsed']:.1f}s",
        f"- 会话内容指纹：`{session.content_digest()[:16]}`",
        "",
        "### 工具调用轨迹",
        "",
    ]
    for step in session.steps:
        lines.append(f"#### 第 {step.round_index} 轮")
        if step.assistant_text:
            # 逐行加引用前缀——否则多行文本只有首行进入块引用。
            quoted = "\n".join(
                f"> {ln}" for ln in (step.assistant_text[:280].splitlines() or [""])
            )
            lines.append(f"\n{quoted}\n")
        for call, res in zip(step.tool_calls, step.results):
            args = json.dumps(dict(call.arguments), ensure_ascii=False)
            lines.append(f"- `{call.name}({args})`")
            lines.append(f"  - status=`{res.status}` handle=`{res.handle}` reason=`{res.reason}`")

    lines += ["", "### LLM 最终陈述", "", session.final_text or "(无)", ""]

    lines += [
        "## 结论对照（务必区分）",
        "",
        "| 维度 | 阶段一（取数） | 阶段二（LLM 分析） |",
        "| --- | --- | --- |",
        "| 执行者 | M7 建议 + M9 取数 + M8 判定 | LLM 自主调用 M2–M9 算法工具 |",
        "| 数据分区 | 确证分区 C | 探索分区 E / 开发评估分区 V |",
        "| 证据地位 | 由系统机械判定等级 | **仅探索性**，LLM 无权授予等级 |",
        "| 产物 | 规律清单 + 取数轨迹 | 工具调用轨迹 + 文本陈述 |",
        "",
        "> **隔离设计**：阶段二的 LLM **看不到**阶段一写入确证分区 C 的内容；"
        "本脚本只把取数的**数量级摘要**作为背景喂给 LLM，不泄漏确证数据本身。",
        "> 因此两份「结论」不构成互相印证，也不应被合并解读——"
        "LLM 的结果需要经过独立确证流程才可能升级为证据。",
        "",
    ]

    (out_dir / "报告.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="M9 取数 × M11 LLM 分析 端到端实测")
    parser.add_argument("output", nargs="?", default=str(ROOT / "demo-output" / "p19-live-acq"),
                        help="输出目录")
    parser.add_argument("--groups", type=int, default=24, help="初始登记的合成批次数")
    parser.add_argument("--seed", type=int, default=7, help="会话盐/分析种子")
    parser.add_argument("--max-rounds", type=int, default=10)
    parser.add_argument("--max-tool-calls", type=int, default=24)
    args = parser.parse_args()

    out_dir = pathlib.Path(args.output)

    print("=" * 72)
    print("M9 取数 × M11 LLM 分析 · 端到端实测")
    print("=" * 72)

    # 前置：先校验 endpoint，避免跑完取数才发现凭据缺失。
    try:
        cfg = EndpointConfig.from_env()
    except Exception as exc:  # noqa: BLE001
        print(f"[ERR] endpoint 未配置：{exc}")
        print("      需要 SDL_LLM_BASE_URL / SDL_LLM_MODEL（密钥置于 SDL_LLM_API_KEY）。")
        return 2
    print(f"endpoint : {cfg.base_url}")
    print(f"model    : {cfg.model}")
    print(f"api_key  : 已提供（明文不出现在任何产物中），配置指纹 {cfg.content_digest()}")
    print()

    # —— 阶段一：自主取数 ——
    print("—— 阶段一：自主取数（M7 建议 → M9 取数 → M1 登记 → M8 判定）——")
    acq = run_acquisition(out_dir / "acquisition", args.groups)
    reg = acq["payload"]["regularities"]
    print(f"  耗时 {acq['elapsed']:.1f}s｜取回 {acq['acquired_batch_count']} 批 / "
          f"{acq['acquired_record_count']} 条记录")
    print(f"  规律清单：支持 {reg.get('supported_count')} 反驳 {reg.get('refuted_count')} "
          f"（知识库 {reg.get('knowledge_version')}）")
    print()

    # —— 阶段二：LLM 驱动分析 ——
    print("—— 阶段二：真实 LLM 驱动分析（探索分区 E/V）——")
    ana = run_analysis(
        out_dir / "analysis", acq,
        seed=args.seed, max_rounds=args.max_rounds, max_tool_calls=args.max_tool_calls,
    )
    session = ana["session"]
    print(f"  stop_reason={session.stop_reason}｜工具调用={session.tool_call_count}｜"
          f"轮次={len(session.steps)}｜耗时={ana['elapsed']:.1f}s")
    for step in session.steps:
        print(f"\n  [第 {step.round_index} 轮] {(step.assistant_text or '')[:100]}")
        for call, res in zip(step.tool_calls, step.results):
            print(f"    → {call.name}({json.dumps(dict(call.arguments), ensure_ascii=False)[:80]})")
            print(f"      ← status={res.status} reason={res.reason}")
    print("\n  LLM 最终陈述：")
    print("    " + (session.final_text or "(无)").replace("\n", "\n    "))

    # —— 合并报告 ——
    write_report(out_dir, acq, ana)
    print(f"\n产物已写入：{out_dir}/ （acquisition/ analysis/ 报告.md）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
