"""Read-only Self Renew analysis; proposed changes stay in a reviewable artifact."""
from renew.prompts import AUDITOR
from renew.runtime import AgentRunner
from renew.repository import RepoTools
from renew.pipeline import validate_findings
from renew.schemas import ANALYSIS
from .bootstrap import ROOT
from .renew_adapter import ChatProvider
from .store import digest

def build_feedback(client, archive, spec, recommendation, *, progress=lambda event: None):
    facts = {"dataset_id": spec.dataset_id, "stop": archive.get("stop"),
        "ledger": archive.get("ledger"), "open_questions": archive.get("open_questions"),
        "strategy": recommendation["selected_method"], "validation_scope": "business discovery run"}
    if client is None:
        return {"mode": "offline", "findings": [], "input_digest": digest(facts),
                "note": "No simulated LLM review is presented as an actual review"}
    import json
    provider = ChatProvider(client.config, max_calls=3, max_total_tokens=60000, progress=progress)
    tools = RepoTools(ROOT / "ogflow", writable=False)
    # Pre-read a fixed scope through the original read-only tools. This avoids
    # an expensive repository-navigation loop before the actual review.
    reads = [("list_files", {"path": "."})] + [("read_file", {"path": name, "start_line": 1, "end_line": 200})
        for name in ("pipeline.py", "sdl_adapter.py")]
    evidence = []
    for name, args in reads:
        result = tools.dispatch(name, args)
        if "error" in result:
            raise ValueError("Self Renew could not obtain the declared review scope")
        evidence.append({"tool": name, "arguments": args, "result": result})
        progress({"phase": "tool", "name": name, "pre_read": True})
    runner = AgentRunner(provider, client.config.model, max_turns=3, max_output_tokens=8192,
        event=lambda role, event: progress({"phase": "tool", **event}))
    response = runner.run(AUDITOR, json.dumps({"task": (
        "Bounded read-only audit. The attached evidence was obtained by list_files and read_file from the actual repository. "
        "Review these two complete files, then call final_result alone now. Do not inspect other files or seek test logs. "
        "Report at most 2 concrete issues using exact quotes from the two files; empty findings are valid. "
        "Keep all prose concise (at most 150 words per finding). State that this is static review only."),
        "run_facts": facts, "read_only_tool_evidence": evidence}, ensure_ascii=False), [], tools.dispatch, ANALYSIS)
    findings, rejected = validate_findings(response["findings"], ROOT / "ogflow")
    return {"mode": "live_self_renew_agent", "findings": findings, "rejected": rejected,
            "project_summary": response["project_summary"], "limitations": response["limitations"],
            "usage": provider.snapshot(), "input_digest": digest(facts), "state": "PROPOSED",
            "note": "Read-only Self Renew audit; accepted source quotes are verified. Findings still require behavioral validation."}

