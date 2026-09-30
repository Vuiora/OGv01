import math
import pytest
from ogflow.statistics import METHOD, make_evaluator
from sdl_m02.expressions import parse, to_ast

def fixture(groups=20):
    rows = [{"values": {"X1": float(i + 1), "Y": float(i + 1)}, "group_ids": {"batch": f"g{i}"}} for i in range(groups)]
    hypotheses = [{"id": name, "model": {"expression": to_ast(parse("X1")), "target": "Y"},
                   "parameters": {"intercept": 0, "slope": slope}}
                  for name, slope in [("good", 1), ("bad", -1)]]
    plan = {"hypotheses": hypotheses, "test_family": [{"id": name, "hypothesis_id": name, "method": METHOD} for name in ("good", "bad")],
        "effect_threshold": 0.1, "preprocessing": {"steps": [{"step": "ogflow_frozen_policy", "target": "Y",
            "group_field": "batch", "constant_baseline": 0, "min_groups": 10}]}}
    return plan, rows

def test_exact_sign_test_uses_each_frozen_model_and_plda():
    plan, rows = fixture()
    result = make_evaluator(group_field="batch", target="Y", min_groups=10)(plan=plan, binding={}, records=rows, alpha=.025)
    assert result["good"]["p_value"] == pytest.approx(2 ** -20)
    assert result["bad"]["p_value"] == 1
    assert result["good"]["effect"] > 0
    assert result["bad"]["effect"] < 0
    assert result["good"]["unit_count"] == 20

def test_ties_are_failures_and_small_group_count_is_unavailable():
    plan, rows = fixture(5)
    result = make_evaluator(group_field="batch", target="Y", min_groups=10)(plan=plan, binding={}, records=rows, alpha=.025)
    assert all(v["p_value"] is None for v in result.values())

@pytest.mark.parametrize("mutation", ["method", "target", "min_groups"])
def test_evaluator_rejects_frozen_policy_mismatch(mutation):
    plan, rows = fixture()
    if mutation == "method":
        plan["test_family"][0]["method"] = "post hoc normal approximation"
    else:
        plan["preprocessing"]["steps"][0][mutation] = "wrong" if mutation == "target" else 5
    with pytest.raises(ValueError):
        make_evaluator(group_field="batch", target="Y", min_groups=10)(plan=plan, binding={}, records=rows, alpha=.025)

