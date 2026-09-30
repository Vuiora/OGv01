"""Synthetic, descriptive demonstration; this is not a statistical test engine."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import statistics


def _save(directory: Path, filename: str, value) -> None:
    (directory / filename).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def synthetic_inputs(seed: int = 20260919) -> tuple[dict, list[dict]]:
    """Return registered design and 120 independent simulated batch effects."""
    spec = {
        "task": {
            "objects": "合成反应实验的新批次；每批8次相关读数",
            "environments": ["lab-A", "lab-B"],
            "time_scope": "2026-01-01至2026-04-30；同生成机制的新批次",
            "claim_types": ["prediction"],
            "target_quantity": "批次等权的基线减候选均方损失差",
            "weighting": "equal_batch",
            "eligibility": "全部登记合成批次及其读数",
        },
        "schema": {
            "version": "1",
            "fields": {
                "X1": {"type": "number", "role": "feature", "unit": "mol/L", "required": True,
                       "missing_allowed": False, "minimum": 0, "range_action": "flag"},
                "X2": {"type": "number", "role": "feature", "unit": "mol/L", "required": True,
                       "missing_allowed": False, "minimum": 0.1, "range_action": "flag"},
                "Y": {"type": "number", "role": "target", "unit": "mol/(L*s)", "required": True,
                      "missing_allowed": False},
            },
            "missing_codes": [None, "NA"],
            "source_required": True,
        },
        "dependence": {
            "record_unit": "reading", "split_unit": "batch", "inference_unit": "batch",
            "group_fields": ["batch"], "namespace": "synthetic-reaction-v1",
            "assumptions": ["independent simulated batches; dependent readings within batch"],
        },
        "split": {"strategy": "grouped", "allocation": {"E": 0.6, "V": 0.2, "C1": 0.2}, "seed": 42},
        "quality": {"version": "1", "notes": ["合成数据；不代表真实测量质量。保留原始记录，不进行插补。"]},
        "confirmation": {"total_alpha": 0.05, "sampling_plan": "24 independent simulated batches",
                         "stopping_rule": "fixed batch count; no optional stopping"},
        "identification_gaps": ["对真实实验的代表性、测量误差和因果解释未被验证"],
    }
    rng = random.Random(seed)
    records = []
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for batch in range(120):
        batch_effect = rng.gauss(0, 0.35)
        for reading in range(8):
            observed = start + timedelta(days=batch, minutes=reading)
            x1, x2 = rng.uniform(0.3, 8), rng.uniform(0.6, 4)
            y = 3 * x1 / x2 + batch_effect + rng.gauss(0, 0.2)
            records.append({
                "record_id": f"synthetic-{batch:03d}-{reading}",
                "group_ids": {"batch": f"batch-{batch:03d}"},
                "environment": "lab-A" if batch % 2 == 0 else "lab-B",
                "event_time": observed.isoformat(),
                "prediction_time": observed.isoformat(),
                "available_time": {"X1": observed.isoformat(), "X2": observed.isoformat(),
                                   "Y": (observed + timedelta(minutes=10)).isoformat()},
                "values": {"X1": x1, "X2": x2, "Y": y},
                "units": {"X1": "mol/L", "X2": "mol/L", "Y": "mol/(L*s)"},
                "source": {"generator": "sdl_m01.demo.synthetic_inputs", "seed": seed, "synthetic": True},
                "measurement_notes": {"synthetic_batch_effect": True, "instrument_calibration": "not_applicable"},
            })
    return spec, records


def _fit_exploration(records: list[dict]) -> dict:
    ratios = [record["values"]["X1"] / record["values"]["X2"] for record in records]
    outcomes = [record["values"]["Y"] for record in records]
    x_mean, y_mean = statistics.fmean(ratios), statistics.fmean(outcomes)
    slope = sum((x - x_mean) * (y - y_mean) for x, y in zip(ratios, outcomes)) / sum((x - x_mean) ** 2 for x in ratios)
    return {"intercept": y_mean - slope * x_mean, "slope": slope, "baseline_mean": y_mean}


def run_demo(output: str | Path) -> dict:
    from . import Module01, initialize
    from .__main__ import save_credentials

    directory = Path(output).resolve()
    directory.mkdir(parents=True, exist_ok=False)
    spec, raw = synthetic_inputs()
    _save(directory, "spec.json", spec)
    _save(directory, "records.json", raw)
    tokens = initialize(directory / "evidence.sqlite3")
    credential_paths = save_credentials(tokens, directory / "credentials")
    custodian = Module01(directory / "evidence.sqlite3", tokens["custodian"])
    explorer = Module01(directory / "evidence.sqlite3", tokens["explorer"])
    confirmer = Module01(directory / "evidence.sqlite3", tokens["confirmer"])
    auditor = Module01(directory / "evidence.sqlite3", tokens["auditor"])
    protocol = custodian.build(spec, raw)
    refs = protocol["resources"]
    exploration, development = explorer.read_dataset(refs["E"]), explorer.read_dataset(refs["V"])
    _save(directory, "E-view.json", exploration)
    _save(directory, "V-view.json", development)
    _save(directory, "E-quality.json", explorer.quality(refs["E"]))
    _save(directory, "V-quality.json", explorer.quality(refs["V"]))
    parameters = _fit_exploration(exploration)
    plan = {
        "protocol_id": protocol["protocol_id"], "round_index": 1,
        "hypotheses": [{"id": "h1", "version": "1",
                        "statement": "合成范围内，比例候选的批次等权均方损失优于固定均值基线",
                        "prediction": "E[baseline_batch_mse - candidate_batch_mse] > 0.1",
                        "scope": spec["task"]["objects"],
                        "representation": {"formula": "X1 / X2", "domain": "X2 >= 0.1", "out_of_domain": "record failure; no post-hoc exclusion"},
                        "model": "intercept + slope * (X1 / X2); baseline = baseline_mean", "parameters": parameters}],
        "preprocessing": {"steps": [{"name": "fixed ratio", "definition": "X1/X2; X2>=0.1"}], "fit_dataset_refs": [refs["E"]]},
        "primary_metric": "equal_batch_mean_squared_loss_gain",
        "test_family": [{"id": "t1", "hypothesis_id": "h1", "null": "expected batch loss gain <= 0.1",
                         "alternative": "expected batch loss gain > 0.1",
                         "method": "one-sided batch-level test with Holm correction; external M6 implementation required"}],
        "effect_threshold": 0.1,
        "sampling_plan": spec["confirmation"]["sampling_plan"],
        "stopping_rule": spec["confirmation"]["stopping_rule"],
        "inference_unit": spec["dependence"]["inference_unit"],
        "eligibility": spec["task"]["eligibility"], "quality_rules_version": spec["quality"]["version"],
        "assumptions": [{"name": spec["dependence"]["assumptions"][0],
                         "justification": "生成器分别抽取各批次效应；这仅描述合成机制，不能证明真实样本独立。"}],
    }
    _save(directory, "frozen-plan.json", plan)
    binding = confirmer.bind_confirmation(refs["C1"], plan)
    _save(directory, "binding.json", binding)
    confirmation = confirmer.consume_confirmation(binding["binding_id"])
    gains = defaultdict(list)
    for record in confirmation:
        values = record["values"]
        prediction = parameters["intercept"] + parameters["slope"] * values["X1"] / values["X2"]
        gain = (values["Y"] - parameters["baseline_mean"]) ** 2 - (values["Y"] - prediction) ** 2
        gains[record["group_ids"]["batch"]].append(gain)
    evaluation = {
        "status": "inconclusive",
        "metrics": {"descriptive_equal_batch_loss_gain": statistics.fmean(statistics.fmean(group) for group in gains.values()),
                    "evaluated_batches": len(gains), "evaluated_records": len(confirmation), "inferential_test_executed": False},
        "notes": "仅为完整证据生命周期演示。指标是合成数据的描述性计算，未执行冻结方案中的统计检验；没有p值、显著性结论或已确证发现。",
        "code_version": "sdl_m01.demo/0.1.0",
    }
    confirmer.record_evaluation(binding["binding_id"], evaluation)
    confirmer.release_results(binding["binding_id"])
    released = explorer.results(binding["binding_id"])
    archived = custodian.archive_confirmation(binding["binding_id"])
    _save(directory, "external-evaluation.json", evaluation)
    _save(directory, "released-result.json", released)
    _save(directory, "historical-reference.json", archived)
    _save(directory, "public-protocol.json", explorer.describe(protocol["protocol_id"]))
    _save(directory, "audit-ledger.json", auditor.ledger())
    integrity = auditor.verify_integrity()
    _save(directory, "integrity.json", integrity)
    summary = {"demonstration_only": True, "output_dir": str(directory), "protocol_id": protocol["protocol_id"],
               "binding_id": binding["binding_id"], "generated_records": len(raw), "generated_batches": 120,
               "partition_batches": {"E": len({r["group_ids"]["batch"] for r in exploration}),
                                     "V": len({r["group_ids"]["batch"] for r in development}), "C1": len(gains)},
               "confirmation_status": "inconclusive", "credentials": credential_paths,
               "integrity": integrity}
    _save(directory, "demo-summary.json", summary)
    (directory / "说明.md").write_text(
        "# 合成演示输出\n\n本目录演示120个批次、每批8条读数的证据协议。E/V/C1分别为72/24/24个批次。\n\n"
        "`records.json`、`evidence.sqlite3`、`credentials/`和完整账本属于本地保管/管理资料，不应交给不可信探索程序。"
        "`credentials/`每个文件仅存放一个角色令牌；不要提交版本库或写入提示词。\n\n"
        "候选参数仅由E视图拟合；冻结后才读取C1，计算描述性误差差异，并记录`inconclusive`。"
        "没有执行统计检验，也没有声称发现已确证。结果发布后建立historical视图，原C1已经used，不能重新用于新确证。\n\n"
        "demo在单一受信进程内扮演四种角色，仅验证协议接口。实际隔离需要服务端边界、账户与文件系统权限。\n",
        encoding="utf-8",
    )
    return summary
