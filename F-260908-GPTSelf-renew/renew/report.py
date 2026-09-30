from pathlib import Path


LABELS = {"verified": "已通过测试与独立审查", "implemented_unverified": "已实现，待运行测试",
          "analyzed": "分析与需求已生成", "no_changes": "本轮无可交付改动", "needs_attention": "部分需求未通过",
          "integration_failed": "集成测试失败", "failed": "运行失败", "interrupted": "已中断",
          "rejected": "验收未通过", "blocked_dependency": "前置需求未完成"}


def write_report(path: Path, state):
    lines = ["# Self Renew · 研发报告", "", f"状态：**{LABELS.get(state['status'], state['status'])}**", "",
             f"模式：{'离线演示（智能体决策是固定模拟数据，文件修改与测试真实执行）' if state['mode'] == 'demo' else '标准 Codex CLI' if state['mode'] == 'codex_cli' else 'OpenAI API'}", "",
             f"源项目：`{state.get('source', '')}`", "", f"交付副本：`{state.get('workspace', '')}`", "",
             "源项目保持不变；changes.patch 为集成改动，各需求目录保留候选代码、审查和测试日志。", ""]
    if state.get("error"):
        lines += [f"运行错误：{state['error']}", ""]
    for cycle in state["cycles"]:
        lines += [f"## 第 {cycle['number']} 轮", ""]
        for summary in cycle.get("analysis_summaries", []):
            lines += [summary["summary"], ""]
            lines += [f"- 审查限制：{x}" for x in summary["limitations"]]
            lines.append("")
        for f in cycle["findings"]:
            lines += [f"### {f['id']} · {'缺陷' if f['kind'] == 'fix' else '创新假设'} · {f['title']}", "",
                      f["problem"], "", f"价值：{f['expected_value']}", "",
                      f"优先分：{f['priority_score']}；风险：{f['risk']}；置信度：{f['confidence']}", ""]
            if f["hypothesis"]:
                lines += [f"待验证：{f['hypothesis']}", ""]
            lines += [f"- 代码证据：`{e['path']}:{e['line']}` — {e['quote']}" for e in f["evidence"]]
            lines += ["", "引用已核对；行为缺陷和创新价值仍以测试、审查或实际实验验证。", ""]
        if cycle["rejected_findings"]:
            lines += [f"有 {len(cycle['rejected_findings'])} 条发现未通过证据检查，未进入开发。详见 run.json。", ""]
        plan = cycle.get("plan")
        if plan:
            lines += ["### 需求", "", plan["strategy"], ""]
            for req in plan["requirements"]:
                lines += [f"**{req['id']} · {req['title']}**", "", req["outcome"], ""]
                lines += [f"- {criterion}" for criterion in req["acceptance_criteria"]]
                lines += ["", f"开发范围：{', '.join(req['allowed_files'])}", ""]
            lines += [f"- 暂缓：{item}" for item in plan["deferred"]]
            lines.append("")
        if cycle.get("stop_reason"):
            lines += [cycle["stop_reason"], ""]
    lines += ["## 开发交付", ""]
    for delivery in state["deliveries"]:
        req = delivery["requirement"]
        lines += [f"- 第 {delivery['cycle']} 轮 / {req['id']} {req['title']}：{LABELS.get(delivery['status'], delivery['status'])}"]
        for attempt in delivery["attempts"]:
            checks = attempt["checks"]
            review = attempt.get("review")
            summary = review["summary"] if review else f"阶段：{attempt.get('stage', '进行中')}，尚未完成审查"
            lines += [f"  - 尝试 {attempt['attempt']}：{summary}；执行 {len(checks)} 条检查命令。"]
    usage = state.get("usage", {})
    calls_label = "Codex CLI 调用" if state.get("mode") == "codex_cli" else "API 请求尝试"
    lines += ["", f"{calls_label}：{usage.get('calls', 0)}；累计 token：{usage.get('total_tokens', 0)}。", "",
              "调用和 token 上限是单次运行的执行限制，不是美元计费上限。", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
