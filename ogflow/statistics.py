"""Preregistered group sign test on frozen predictions, with PLDA loss reduction."""
import ast
import json
import math
import numpy as np
from scipy.stats import binom
from plda import PLDAModule, Program, Task, Tensor
from sdl_m02.expressions import from_ast
from sdl_m03.equations import evaluate_expression

METHOD = ("ogflow group_sign_test v1: compute frozen candidate vs frozen E-mean baseline MSE "
          "within each declared independent group; one-sided exact binomial tail for number "
          "of groups with MSE improvement strictly greater than delta, null probability <= 0.5; "
          "ties count as failures; equal group weights; no refit; SDL Holm correction and alpha spending.")

def parse_model(text):
    if isinstance(text, dict):
        return text
    output = {}
    for part in str(text).split("; "):
        key, sep, value = part.partition("=")
        if sep:
            try:
                output[key.strip()] = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                output[key.strip()] = value
    return output

def frozen_prediction(hypothesis, values):
    model = parse_model(hypothesis["model"])
    expression = model.get("expression") or hypothesis.get("representation", {}).get("expression")
    if isinstance(expression, str):
        expression = json.loads(expression)
    coefficients = hypothesis.get("parameters") or model.get("coefficients") or {}
    if "intercept" not in coefficients or "slope" not in coefficients:
        raise ValueError("Frozen model lacks intercept/slope")
    return float(coefficients["intercept"]) + float(coefficients["slope"]) * evaluate_expression(from_ast(expression), values)

def loss_difference(observed, predicted, baseline, module):
    n = len(observed)
    inputs = {"y": Tensor.from_values(observed), "p": Tensor.from_values(predicted),
              "b": Tensor.from_values([baseline] * n)}
    tasks = (
        Task("neg_y", "scale", ("y",), {"factor": -1}, devices=("CPU",)),
        Task("residual", "add", ("p", "neg_y"), devices=("CPU",)),
        Task("null_residual", "add", ("b", "neg_y"), devices=("CPU",)),
        Task("square", "multiply", ("residual", "residual"), devices=("CPU",)),
        Task("null_square", "multiply", ("null_residual", "null_residual"), devices=("CPU",)),
        Task("negative_square", "scale", ("square",), {"factor": -1}, devices=("CPU",)),
        Task("gain", "add", ("null_square", "negative_square"), devices=("CPU",)),
    )
    result = module.run(Program(inputs, tasks, ("gain",)))
    if result.state != "SUCCEEDED":
        raise RuntimeError("PLDA confirmation computation failed")
    return result.outputs["gain"].values()

def make_evaluator(*, group_field, target, min_groups):
    def evaluate(*, plan, binding, records, alpha):
        if any(entry["method"] != METHOD for entry in plan["test_family"]):
            raise ValueError("Frozen statistical method differs from executing method")
        policy = next((p for p in plan["preprocessing"]["steps"]
                       if isinstance(p, dict) and p.get("step") == "ogflow_frozen_policy"), None)
        if policy is None or policy["group_field"] != group_field or policy["target"] != target or policy["min_groups"] != min_groups:
            raise ValueError("Frozen confirmation policy is missing or mismatched")
        baseline, delta = float(policy["constant_baseline"]), float(plan["effect_threshold"])
        if not math.isfinite(baseline) or not math.isfinite(delta):
            raise ValueError("Nonfinite frozen confirmation parameters")
        hypotheses = {h["id"]: h for h in plan["hypotheses"]}
        output = {}
        with PLDAModule() as module:
            for entry in plan["test_family"]:
                hypothesis = hypotheses[entry["hypothesis_id"]]
                # Every hypothesis is evaluated independently; no copying the first model's p-value.
                observed = [float(r["values"][target]) for r in records]
                try:
                    predicted = [frozen_prediction(hypothesis, r["values"]) for r in records]
                    if not all(math.isfinite(p) for p in predicted):
                        raise ValueError("nonfinite prediction")
                except (ValueError, TypeError, KeyError, OverflowError, ZeroDivisionError):
                    output[entry["id"]] = {"p_value": None}
                    continue
                if not records:
                    output[entry["id"]] = {"p_value": None}
                    continue
                improvements = loss_difference(observed, predicted, baseline, module)
                buckets = {}
                for row, improvement in zip(records, improvements):
                    group = str(row["group_ids"][group_field])
                    buckets.setdefault(group, []).append(improvement)
                differences = [float(np.mean(values)) for values in buckets.values()]
                if len(differences) < min_groups:
                    output[entry["id"]] = {"p_value": None}
                    continue
                wins = sum(value > delta for value in differences)
                output[entry["id"]] = {"p_value": float(binom.sf(wins - 1, len(differences), 0.5)),
                    "effect": float(np.median(differences)), "delta": delta, "unit_count": len(differences),
                    "sample_size": len(records)}
        return output
    return evaluate

