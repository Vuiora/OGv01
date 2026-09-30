"""Bounded autonomous discovery → requirements → implementation → independent review."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import tempfile
import threading

from .config import Config
from . import prompts, schemas
from .repository import RepoTools, create_snapshot, diff_workspace, run_checks
from .runtime import AgentRunner


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def checks_passed(checks):
    if not checks:
        return False
    for result in checks:
        output = str(result.get("stdout", "")) + str(result.get("stderr", ""))
        if result.get("exit_code") != 0 or result.get("timed_out"):
            return False
        if re.search(r"Ran 0 tests|no tests ran|collected 0 items", output, re.I):
            return False
    return True


def safe_relative(path):
    if not isinstance(path, str) or not path or "\\" in path or ":" in path:
        return False
    if PureWindowsPath(path).is_absolute() or PurePosixPath(path).is_absolute():
        return False
    parts = path.split("/")
    return all(part not in ("", ".", "..") and not any(c in part for c in '*?[]\x00') for part in parts)


def fingerprint(finding):
    # Stable enough to avoid blindly retrying an identical finding within this run.
    data = [finding["kind"], finding["title"].strip().casefold(),
            sorted((x["path"], x["quote"].strip()) for x in finding["evidence"])]
    return hashlib.sha256(json.dumps(data, ensure_ascii=False).encode()).hexdigest()


def validate_findings(findings, root):
    verified, rejected = [], []
    reader = RepoTools(root)
    for finding in findings:
        reasons = []
        for evidence in finding["evidence"]:
            if not safe_relative(evidence["path"]) or not evidence["quote"].strip():
                reasons.append("证据路径/引用无效")
                continue
            result = reader.dispatch("read_file", {"path": evidence["path"],
                                     "start_line": evidence["line"], "end_line": evidence["line"]})
            if "error" in result:
                reasons.append("证据文件不可读")
                continue
            try:
                lines = (root / evidence["path"]).read_text(encoding="utf-8-sig").splitlines()
                if evidence["line"] > len(lines) or evidence["quote"] not in lines[evidence["line"] - 1]:
                    reasons.append("引用与指定行原文不一致")
            except (OSError, UnicodeError):
                reasons.append("证据文件不是 UTF-8 文本")
        if finding["kind"] == "innovation" and not finding["hypothesis"].strip():
            reasons.append("创新缺少待验证假设")
        if reasons:
            rejected.append({"finding": finding, "reasons": reasons})
        else:
            finding = dict(finding)
            penalty = {"low": 1, "medium": 1.5, "high": 2.5}[finding["risk"]]
            finding["priority_score"] = round(finding["impact"] * finding["confidence"] / (finding["effort"] * penalty), 3)
            finding["evidence_status"] = "quote_verified_not_behavior_proven"
            verified.append(finding)
    return sorted(verified, key=lambda x: x["priority_score"], reverse=True), rejected


def order_requirements(requirements, findings):
    by_finding = {f["id"]: f for f in findings}
    by_id = {}
    for req in requirements:
        if not re.fullmatch(r"R[1-9][0-9]*", req["id"]) or req["id"] in by_id:
            raise ValueError("需求 ID 必须唯一且形如 R1")
        _validate_requirement(req, by_finding)
        req["priority_score"] = max(by_finding[ref]["priority_score"] for ref in req["finding_ids"])
        by_id[req["id"]] = req
    ordered, pending = [], dict(by_id)
    while pending:
        ready = [req for req in pending.values() if all(dep in {x['id'] for x in ordered} for dep in req["dependencies"])]
        if not ready:
            raise ValueError("需求依赖包含循环或未知 ID")
        ready.sort(key=lambda x: x["priority_score"], reverse=True)
        chosen = ready[0]
        ordered.append(chosen)
        del pending[chosen["id"]]
    return ordered


def _validate_requirement(req, by_finding):
    if not req["finding_ids"] or not all(ref in by_finding for ref in req["finding_ids"]):
        raise ValueError(f"{req['id']} 引用了无证据发现")
    if not all(by_finding[ref]["kind"] == req["kind"] for ref in req["finding_ids"]):
        raise ValueError("需求类型必须与其发现一致")
    if not all(safe_relative(path) for path in req["allowed_files"]):
        raise ValueError("需求 allowed_files 必须是具体相对文件路径")
    if any(not criterion.strip() for criterion in req["acceptance_criteria"]):
        raise ValueError("验收条件不能为空")
    if len(set(req["acceptance_criteria"])) != len(req["acceptance_criteria"]):
        raise ValueError("验收条件不能重复")


def filter_and_order_requirements(requirements, findings):
    """Keep independently valid planner output and record rejected requirements."""
    by_finding = {f["id"]: f for f in findings}
    accepted, rejected, seen_ids = [], [], set()
    for req in requirements:
        try:
            if not re.fullmatch(r"R[1-9][0-9]*", req["id"]) or req["id"] in seen_ids:
                raise ValueError("需求 ID 必须唯一且形如 R1")
            seen_ids.add(req["id"])
            _validate_requirement(req, by_finding)
            item = dict(req)
            item["priority_score"] = max(by_finding[ref]["priority_score"] for ref in item["finding_ids"])
            accepted.append(item)
        except (KeyError, TypeError, ValueError) as exc:
            rejected.append({"requirement": req, "reason": str(exc)})

    # If a prerequisite was rejected or never returned, its dependents cannot run either.
    while True:
        accepted_ids = {req["id"] for req in accepted}
        blocked = [req for req in accepted if any(dep not in accepted_ids for dep in req["dependencies"])]
        if not blocked:
            break
        for req in blocked:
            accepted.remove(req)
            rejected.append({"requirement": req, "reason": "依赖引用了未通过校验或不存在的需求"})

    ordered, pending = [], {req["id"]: req for req in accepted}
    while pending:
        completed = {req["id"] for req in ordered}
        ready = [req for req in pending.values() if all(dep in completed for dep in req["dependencies"])]
        if not ready:
            for req in pending.values():
                rejected.append({"requirement": req, "reason": "需求依赖包含循环"})
            break
        ready.sort(key=lambda req: req["priority_score"], reverse=True)
        chosen = ready[0]
        ordered.append(chosen)
        del pending[chosen["id"]]
    return ordered, rejected


def review_passed(review, requirement):
    criteria = review["criteria"]
    return (review["approved"] and not review["issues"]
            and sorted(x["criterion"] for x in criteria) == sorted(requirement["acceptance_criteria"])
            and all(x["passed"] and x["evidence"].strip() for x in criteria))


class Pipeline:
    def __init__(self, config: Config, provider, output: Path, *, execute_tests=False, analyze_only=False, demo=False, progress=None):
        self.config = config.validate()
        self.provider = provider
        self.output = Path(output).resolve()
        self.execute_tests = execute_tests
        self.analyze_only = analyze_only
        self.demo = demo
        self.progress = progress or (lambda text: None)
        self._lock = threading.Lock()
        self.state = {"version": 1, "mode": "demo" if demo else getattr(provider, "mode", "openai_api"),
                      "status": "starting", "cycles": [], "deliveries": [],
                      "started_at": datetime.now(timezone.utc).isoformat()}

    def event(self, role, detail):
        match = re.search(r"角色 ([A-Z]+)", role)
        role = match.group(1) if match else role[:80]
        with self._lock:
            with (self.output / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"time": datetime.now(timezone.utc).isoformat(),
                                         "role": role, "event": detail}, ensure_ascii=False) + "\n")

    def persist(self):
        self.state["usage"] = self.provider.snapshot()
        save_json(self.output / "run.json", self.state)
        from .report import write_report
        write_report(self.output / "report.md", self.state)

    def check(self, root):
        """Test a disposable copy; generated files never enter the delivery."""
        if not self.execute_tests or not self.config.test_commands:
            return []
        checks_dir = self.output / "checks"
        checks_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="check-", dir=checks_dir) as temporary:
            checked = Path(temporary) / "project"
            manifest = create_snapshot(root, checked)
            original = {path: (checked / path).read_bytes() for path in manifest["files"]}
            results = run_checks(checked, self.config.test_commands, self.config.test_timeout)
            mutated = [path for path, content in original.items()
                       if not (checked / path).is_file() or (checked / path).is_symlink()
                       or (checked / path).read_bytes() != content]
            for result in results:
                result["cwd"] = str(checked)
                result["workspace_type"] = "disposable_test_copy"
            if mutated:
                results.append({"command": ["self-renew:source-integrity"], "exit_code": 1,
                                "stdout": "", "stderr": "测试改写了输入项目文件，无法验证原候选版本：" + ", ".join(mutated[:30]),
                                "timed_out": False})
            return results

    def agent(self, name, instructions, prompt, root, schema, *, writable=False, allowed_files=None):
        model = getattr(self.config, f"{name}_model")
        tools = RepoTools(root, writable=writable)

        def dispatch(tool, args):
            if tool == "write_file" and allowed_files is not None and args.get("path") not in allowed_files:
                return {"error": "此文件不在本需求 allowed_files 中；只能修改已分配文件"}
            return tools.dispatch(tool, args)

        if hasattr(self.provider, "for_workspace"):
            if getattr(self.provider, "mode", None) == "codex_cli":
                provider = self.provider.for_workspace(root, writable=writable,
                                                       execute_tests=self.execute_tests)
            else:
                provider = self.provider.for_workspace(root, writable=writable)
        else:
            provider = self.provider
        runner = AgentRunner(provider, model, max_turns=self.config.max_turns,
                             max_output_tokens=self.config.max_output_tokens, event=self.event)
        return runner.run(instructions, json.dumps(prompt, ensure_ascii=False), tools.specs(), dispatch, schema)

    def run(self, source: Path):
        source = Path(source).resolve()
        if not source.is_dir():
            raise ValueError(f"项目目录不存在: {source}")
        if self.output == source or source in self.output.parents:
            raise ValueError("输出目录必须位于被分析项目之外，以免递归复制")
        if self.output.exists():
            raise ValueError("输出目录已存在；请使用新的目录以保留历史记录")
        self.output.mkdir(parents=True)
        self.state.update(source=str(source), output=str(self.output), config=self.config.to_dict(),
                          tests_enabled=self.execute_tests)
        try:
            self.progress("读取项目并创建隔离副本…")
            snapshot = self.output / "source"
            manifest = create_snapshot(source, snapshot, exclude=tuple(self.config.exclude))
            save_json(self.output / "snapshot.json", manifest)
            self.state["snapshot"] = {"files": len(manifest["files"]), "skipped": manifest.get("skipped", [])}
            if not manifest["files"]:
                raise ValueError("项目中没有可分析文件")
            workspace = self.output / "workspace"
            create_snapshot(snapshot, workspace)
            self.state["workspace"] = str(workspace)
            self.state["status"] = "analyzing"
            self.persist()
            baseline = self.check(workspace)
            save_json(self.output / "baseline-tests.json", baseline)
            seen = set()
            for cycle_number in range(1, self.config.max_cycles + 1):
                cycle_dir = self.output / f"cycle-{cycle_number}"
                cycle_dir.mkdir()
                cycle = {"number": cycle_number, "findings": [], "rejected_findings": [], "plan": None}
                self.state["cycles"].append(cycle)
                self.progress(f"第 {cycle_number} 轮：并行发现不足和创新机会…")
                discovery_input = {"task": "自主发现项目改进方向", "files": manifest["files"][:1200],
                                   "file_list_truncated": len(manifest["files"]) > 1200,
                                   "previously_considered": list(seen), "baseline_checks": baseline,
                                   "remaining_scope": "聚焦少量可直接实施的改进"}
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(self.agent, "analyst", role, discovery_input, workspace, schemas.ANALYSIS)
                               for role in (prompts.AUDITOR, prompts.INNOVATOR)]
                    analyses = [future.result() for future in futures]
                save_json(cycle_dir / "analysis.json", analyses)
                raw = []
                for prefix, kind, analysis in zip(("A", "I"), ("fix", "innovation"), analyses):
                    for index, finding in enumerate(analysis["findings"], 1):
                        finding["id"] = f"{prefix}{index}"
                        if finding["kind"] != kind:
                            cycle["rejected_findings"].append({"finding": finding, "reasons": ["分析角色与发现类型不符"]})
                        else:
                            raw.append(finding)
                findings, rejected = validate_findings(raw, workspace)
                findings = [f for f in findings if fingerprint(f) not in seen]
                cycle["findings"] = findings
                cycle["rejected_findings"].extend(rejected)
                cycle["analysis_summaries"] = [{"summary": a["project_summary"], "limitations": a["limitations"]} for a in analyses]
                self.persist()
                if not findings:
                    cycle["stop_reason"] = "没有新的、引用核对通过的发现"
                    break
                self.progress("将有证据的发现转成需求与验收条件…")
                plan_input = {"findings": findings, "max_requirements": self.config.max_requirements,
                              "project_summaries": cycle["analysis_summaries"]}
                plan = self.agent("planner", prompts.PLANNER, plan_input, workspace, schemas.PLAN)
                ordered, rejected_requirements = filter_and_order_requirements(plan["requirements"], findings)
                cycle["rejected_requirements"] = rejected_requirements
                plan["deferred"].extend(
                    f"{item['requirement'].get('id', '未知需求')}：规划结果未通过校验（{item['reason']}）"
                    for item in rejected_requirements
                )
                selected = ordered[:self.config.max_requirements]
                plan["requirements"] = selected
                plan["deferred"].extend(f"{r['id']}: {r['title']}（本轮需求上限）" for r in ordered[self.config.max_requirements:])
                cycle["plan"] = plan
                save_json(cycle_dir / "requirements.json", plan)
                self.persist()
                if self.analyze_only:
                    self.state["status"] = "analyzed"
                    break
                integrated = set()
                accepted_count = 0
                for req in selected:
                    if any(dep not in integrated for dep in req["dependencies"]):
                        self.state["deliveries"].append({"cycle": cycle_number, "requirement": req,
                                                         "status": "blocked_dependency", "attempts": []})
                        self.persist()
                        continue
                    delivery = self.develop(req, workspace, cycle_dir, cycle_number)
                    for finding in findings:
                        if finding["id"] in req["finding_ids"]:
                            seen.add(fingerprint(finding))
                    if delivery["status"] in ("verified", "implemented_unverified"):
                        # Only copy assigned files; candidate tests cannot accidentally copy unrelated artifacts.
                        for path in delivery["integrated_files"]:
                            target = workspace / path
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(Path(delivery["candidate"]) / path, target)
                        integrated.add(req["id"])
                        accepted_count += 1
                    self.persist()
                if not accepted_count:
                    cycle["stop_reason"] = "本轮没有集成改动，停止继续消耗预算"
                    break
                baseline = self.check(workspace)
                save_json(cycle_dir / "integration-tests.json", baseline)
                if self.execute_tests and self.config.test_commands and not checks_passed(baseline):
                    self.state["status"] = "integration_failed"
                    break
                manifest = {"files": sorted(str(p.relative_to(workspace)).replace("\\", "/") for p in workspace.rglob("*") if p.is_file())}
            if self.state["status"] not in ("analyzed", "integration_failed"):
                statuses = [x["status"] for x in self.state["deliveries"]]
                self.state["status"] = ("verified" if statuses and all(s == "verified" for s in statuses)
                                         else "needs_attention" if any(s not in ("verified", "implemented_unverified") for s in statuses)
                                         else "implemented_unverified" if statuses else "no_changes")
            (self.output / "changes.patch").write_text(diff_workspace(snapshot, workspace), encoding="utf-8", newline="")
            self.state["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.persist()
            return self.state
        except BaseException as exc:
            self.state["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
            self.state["error"] = str(exc)
            self.state["finished_at"] = datetime.now(timezone.utc).isoformat()
            self.persist()
            raise

    def develop(self, requirement, workspace, cycle_dir, cycle_number):
        directory = cycle_dir / requirement["id"]
        directory.mkdir()
        candidate = directory / "candidate"
        create_snapshot(workspace, candidate)
        delivery = {"cycle": cycle_number, "requirement": requirement, "candidate": str(candidate),
                    "status": "developing", "attempts": [], "integrated_files": []}
        preserved = [d["requirement"] for d in self.state["deliveries"]
                     if d["status"] in ("verified", "implemented_unverified")]
        review_requirement = dict(requirement)
        review_requirement["acceptance_criteria"] = list(dict.fromkeys(
            requirement["acceptance_criteria"] + [c for req in preserved for c in req["acceptance_criteria"]]))
        self.state["deliveries"].append(delivery)
        self.persist()
        feedback = None
        for attempt in range(1, self.config.max_attempts + 1):
            self.progress(f"{requirement['id']} · {requirement['title']}：开发 / 验收 {attempt}/{self.config.max_attempts}")
            record = {"attempt": attempt, "stage": "developing", "development": None, "checks": [], "review": None}
            delivery["attempts"].append(record)
            self.persist()
            result = self.agent("developer", prompts.DEVELOPER,
                                {"requirement": requirement, "feedback": feedback, "preserved_requirements": preserved}, candidate,
                                schemas.DEVELOPMENT, writable=True, allowed_files=requirement["allowed_files"])
            record.update(development=result, stage="testing")
            save_json(directory / f"attempt-{attempt}.json", record)
            self.persist()
            checks = self.check(candidate)
            record.update(checks=checks, stage="reviewing")
            save_json(directory / f"attempt-{attempt}.json", record)
            self.persist()
            diff = diff_workspace(workspace, candidate)
            (directory / f"attempt-{attempt}.patch").write_text(diff, encoding="utf-8", newline="")
            review = self.agent("reviewer", prompts.REVIEWER,
                                {"requirement": review_requirement, "preserved_requirements": preserved, "development_summary": result,
                                 "diff": diff[:100000], "diff_truncated": len(diff) > 100000,
                                 "checks": checks, "tests_executed": bool(checks)}, candidate, schemas.REVIEW)
            record.update(review=review, stage="reviewed")
            save_json(directory / f"attempt-{attempt}.json", record)
            changed = [path for path in requirement["allowed_files"]
                       if (candidate / path).is_file() and
                       (not (workspace / path).exists() or (workspace / path).read_bytes() != (candidate / path).read_bytes())]
            candidate_files = {str(path.relative_to(candidate)).replace("\\", "/")
                               for path in candidate.rglob("*") if path.is_file()}
            workspace_files = {str(path.relative_to(workspace)).replace("\\", "/")
                              for path in workspace.rglob("*") if path.is_file()}
            out_of_scope = []
            for path in sorted((candidate_files | workspace_files) - set(requirement["allowed_files"])):
                candidate_path, workspace_path = candidate / path, workspace / path
                if (candidate_path.is_file() != workspace_path.is_file()
                        or (candidate_path.is_file() and candidate_path.read_bytes() != workspace_path.read_bytes())):
                    out_of_scope.append(path)
            issues = []
            if not changed:
                issues.append("没有实际代码改动")
            if out_of_scope:
                issues.append("候选版本改动了 allowed_files 之外的文件：" + ", ".join(out_of_scope[:20]))
            if not review_passed(review, review_requirement):
                issues.append("独立验收未逐项通过")
            if self.execute_tests and self.config.test_commands and not checks_passed(checks):
                issues.append("执行器测试未通过（含超时或空测试）")
            if not issues:
                delivery["status"] = "verified" if checks_passed(checks) else "implemented_unverified"
                delivery["integrated_files"] = changed
                save_json(directory / "delivery.json", delivery)
                return delivery
            feedback = {"issues": issues, "review": review, "checks": checks}
        delivery["status"] = "rejected"
        delivery["feedback"] = feedback
        save_json(directory / "delivery.json", delivery)
        return delivery
