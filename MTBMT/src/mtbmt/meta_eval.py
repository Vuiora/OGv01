from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, DefaultDict
from collections import defaultdict

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder


def _normalize_method_name(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return ""
    if n.startswith("mutual_info"):
        return "mutual_info"
    if n in {"dcor", "distance_correlation"}:
        return "distance_correlation"
    if n in {"perm", "permutation_importance"}:
        return "permutation_importance"
    return n


def _flatten_features(meta_features: Dict[str, Any], trajectory_features: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    x = dict(meta_features or {})
    if trajectory_features:
        for k, v in trajectory_features.items():
            x[f"traj__{k}"] = v
    return x


def _safe_float(v: Any, default: float = float("nan")) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def objective_j(ev: Dict[str, Any], w_utility: float, w_stability: float, w_cost: float) -> float:
    cv = _safe_float(ev.get("cv_score_mean"), default=float("nan"))
    stab = _safe_float(ev.get("stability_jaccard"), default=0.0)
    runtime = _safe_float(ev.get("runtime_sec"), default=0.0)
    if not math.isfinite(cv):
        return float("-inf")
    runtime = max(runtime, 0.0)
    return (w_utility * cv) + (w_stability * stab) - (w_cost * math.log1p(runtime))


def _iter_experience(path: str | Path) -> Iterable[Dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except Exception:
                continue


def build_dataset(
    experience_path: str | Path,
    *,
    w_utility: float,
    w_stability: float,
    w_cost: float,
    label_target: str,
    aggregate_by_dataset: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str], List[Dict[str, Any]]]:
    raw_rows: List[Dict[str, Any]] = []
    raw_meta: List[Dict[str, Any]] = []

    for obj in _iter_experience(experience_path):
        dataset_id = str(obj.get("dataset_id", "") or "")
        mf = obj.get("meta_features", {}) or {}
        tf = obj.get("trajectory_features", None)
        evaluations = obj.get("evaluations", {}) or {}

        norm_evals: Dict[str, Dict[str, Any]] = {}
        for k, ev in evaluations.items():
            ev = dict(ev or {})
            name0 = str(ev.get("method_name", k) or k)
            name = _normalize_method_name(name0)
            if not name:
                continue
            ev["method_name"] = name
            norm_evals[name] = ev

        if len(norm_evals) < 2:
            continue

        x = _flatten_features(mf, tf)
        times = {m: _safe_float(ev.get("runtime_sec"), default=0.0) for m, ev in norm_evals.items()}
        cvs = {m: _safe_float(ev.get("cv_score_mean"), default=float("nan")) for m, ev in norm_evals.items()}
        objs = {m: objective_j(ev, w_utility, w_stability, w_cost) for m, ev in norm_evals.items()}
        raw_rows.append(x)
        raw_meta.append(
            {
                "dataset_id": dataset_id,
                "methods": sorted(norm_evals.keys()),
                "times": times,
                "cvs": cvs,
                "objs": objs,
                "full_time": float(sum(max(t, 0.0) for t in times.values())),
            }
        )

    if not aggregate_by_dataset:
        X_rows: List[Dict[str, Any]] = []
        y: List[str] = []
        meta: List[Dict[str, Any]] = []
        for x, m in zip(raw_rows, raw_meta):
            cvs = m["cvs"]
            objs = m["objs"]
            if label_target == "cv":
                best = max(cvs.items(), key=lambda kv: _safe_float(kv[1], default=float("-inf")))[0]
            else:
                best = max(objs.items(), key=lambda kv: _safe_float(kv[1], default=float("-inf")))[0]
            meta.append(
                {
                    **m,
                    "best_method": best,
                    "best_cv": float(np.nanmax(list(cvs.values()))),
                    "best_obj": float(np.nanmax(list(objs.values()))),
                }
            )
            X_rows.append(x)
            y.append(best)
        return X_rows, y, meta

    by_ds: DefaultDict[str, List[int]] = defaultdict(list)
    for i, m in enumerate(raw_meta):
        ds = str(m.get("dataset_id", "") or "")
        if ds:
            by_ds[ds].append(i)

    X_rows_agg: List[Dict[str, Any]] = []
    y_agg: List[str] = []
    meta_agg: List[Dict[str, Any]] = []

    for ds, idxs in by_ds.items():
        keys = sorted({k for i in idxs for k in raw_rows[i].keys()})
        row_out: Dict[str, Any] = {}
        for k in keys:
            vals = [raw_rows[i].get(k, None) for i in idxs]
            nums: List[float] = []
            first_str: Optional[str] = None
            for v in vals:
                if isinstance(v, (int, float)) and v is not None and math.isfinite(float(v)):
                    nums.append(float(v))
                elif isinstance(v, str) and first_str is None:
                    first_str = v
            if nums:
                row_out[k] = float(np.mean(nums))
            elif first_str is not None:
                row_out[k] = first_str
            else:
                row_out[k] = None

        methods = sorted({mm for i in idxs for mm in raw_meta[i]["methods"]})
        if len(methods) < 2:
            continue
        times: Dict[str, float] = {}
        cvs: Dict[str, float] = {}
        objs: Dict[str, float] = {}
        for mm in methods:
            tvals = [raw_meta[i]["times"].get(mm, float("nan")) for i in idxs]
            cvals = [raw_meta[i]["cvs"].get(mm, float("nan")) for i in idxs]
            ovals = [raw_meta[i]["objs"].get(mm, float("nan")) for i in idxs]
            times[mm] = float(np.nanmean(np.asarray(tvals, dtype=float)))
            cvs[mm] = float(np.nanmean(np.asarray(cvals, dtype=float)))
            objs[mm] = float(np.nanmean(np.asarray(ovals, dtype=float)))

        if label_target == "cv":
            best = max(cvs.items(), key=lambda kv: _safe_float(kv[1], default=float("-inf")))[0]
        else:
            best = max(objs.items(), key=lambda kv: _safe_float(kv[1], default=float("-inf")))[0]

        meta_agg.append(
            {
                "dataset_id": ds,
                "methods": methods,
                "times": times,
                "cvs": cvs,
                "objs": objs,
                "best_method": best,
                "best_cv": float(np.nanmax(list(cvs.values()))),
                "best_obj": float(np.nanmax(list(objs.values()))),
                "full_time": float(sum(max(float(t), 0.0) for t in times.values())),
            }
        )
        X_rows_agg.append(row_out)
        y_agg.append(best)

    return X_rows_agg, y_agg, meta_agg


def make_model(rows: List[Dict[str, Any]], *, random_state: int, n_estimators: int) -> Tuple[Pipeline, List[str]]:
    all_cols = sorted({k for r in rows for k in r.keys()})
    X_df = pd.DataFrame([{c: r.get(c, None) for c in all_cols} for r in rows], columns=all_cols)

    cat_cols = [c for c in all_cols if X_df[c].map(lambda v: isinstance(v, str)).any()]
    num_cols = [c for c in all_cols if c not in cat_cols]

    cat_pipe = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore")),
        ]
    )
    num_pipe = Pipeline(steps=[("impute", SimpleImputer(strategy="median"))])

    pre = ColumnTransformer(
        transformers=[
            ("cat", cat_pipe, cat_cols),
            ("num", num_pipe, num_cols),
        ],
        remainder="drop",
    )

    clf = RandomForestClassifier(
        n_estimators=n_estimators,
        random_state=random_state,
        n_jobs=-1,
        class_weight="balanced",
    )

    model = Pipeline([("pre", pre), ("clf", clf)])
    return model, all_cols


def predict_topk(model: Pipeline, feature_cols: List[str], row: Dict[str, Any], *, k: int = 2) -> List[Tuple[str, float]]:
    r = {c: row.get(c, None) for c in feature_cols}
    X1 = pd.DataFrame([r], columns=feature_cols)
    if not hasattr(model, "predict_proba"):
        pred = str(model.predict(X1)[0])
        return [(pred, 1.0)]
    proba = model.predict_proba(X1)[0]
    classes = getattr(model, "classes_", None)
    if classes is None and hasattr(model, "named_steps") and "clf" in model.named_steps:
        classes = getattr(model.named_steps["clf"], "classes_", None)
    if classes is None:
        pred = str(model.predict(X1)[0])
        return [(pred, 1.0)]
    pairs = [(str(c), float(p)) for c, p in zip(classes, proba)]
    pairs.sort(key=lambda t: t[1], reverse=True)
    return pairs[: max(int(k), 1)]


def evaluate_meta_selector(
    *,
    experience: str | Path = "experience/experience.jsonl",
    seed: int = 0,
    cv_datasets: int = 5,
    rf_estimators: int = 300,
    label_target: str = "objective",
    w_utility: float = 1.0,
    w_stability: float = 0.10,
    w_cost: float = 0.15,
    topn: int = 2,
    aggregate_by_dataset: bool = True,
) -> Dict[str, Any]:
    X_rows, y, meta = build_dataset(
        experience,
        w_utility=w_utility,
        w_stability=w_stability,
        w_cost=w_cost,
        label_target=label_target,
        aggregate_by_dataset=aggregate_by_dataset,
    )
    if not X_rows:
        raise ValueError(f"经验库为空或无法解析：{experience}")

    dataset_ids = sorted({m["dataset_id"] for m in meta if m.get("dataset_id")})
    if len(dataset_ids) < 3:
        raise ValueError(f"可用 dataset_id 太少（{len(dataset_ids)} 个）：{dataset_ids}")

    topn = max(int(topn), 1)
    groups = [m.get("dataset_id", "") for m in meta]
    all_idx = list(range(len(meta)))
    unique_groups = sorted(set(groups))

    if int(cv_datasets) <= 1:
        raise ValueError("evaluate_meta_selector 目前要求 cv_datasets > 1（严格分组评估）")
    if int(cv_datasets) > len(unique_groups):
        raise ValueError(f"--cv-datasets={cv_datasets} 但可用 dataset_id 只有 {len(unique_groups)} 个")

    gkf = GroupKFold(n_splits=int(cv_datasets))

    fold_results: List[Dict[str, Any]] = []

    def eval_one_split(train_idx: List[int], test_idx: List[int]) -> Dict[str, Any]:
        model, feature_cols = make_model([X_rows[i] for i in train_idx], random_state=int(seed), n_estimators=int(rf_estimators))
        X_train = pd.DataFrame([{c: X_rows[i].get(c, None) for c in feature_cols} for i in train_idx], columns=feature_cols)
        y_train = [y[i] for i in train_idx]
        model.fit(X_train, y_train)

        hit1 = 0
        hitn = 0
        regrets_cv: List[float] = []
        regrets_obj: List[float] = []
        full_times: List[float] = []
        chosen_times_top1: List[float] = []
        chosen_times_topn: List[float] = []

        for i in test_idx:
            row = X_rows[i]
            m = meta[i]
            best = m["best_method"]
            top = predict_topk(model, feature_cols, row, k=topn)
            top_methods = [name for name, _p in top]
            pred1 = top_methods[0] if top_methods else ""

            if pred1 == best:
                hit1 += 1
            if best in top_methods:
                hitn += 1

            cvs = m["cvs"]
            objs = m["objs"]
            best_cv = float(m["best_cv"])
            best_obj = float(m["best_obj"])
            pred_cv = float(cvs.get(pred1, float("nan")))
            pred_obj = float(objs.get(pred1, float("-inf")))
            regrets_cv.append(best_cv - pred_cv if math.isfinite(pred_cv) else float("nan"))
            regrets_obj.append(best_obj - pred_obj if math.isfinite(pred_obj) else float("nan"))

            full_times.append(float(m["full_time"]))
            times = m["times"]
            chosen_times_top1.append(float(times.get(pred1, 0.0)))
            chosen_times_topn.append(float(sum(times.get(mm, 0.0) for mm in dict.fromkeys(top_methods).keys())))

        n_test_rows = len(test_idx)
        ft = np.asarray(full_times, dtype=float)
        t1 = np.asarray(chosen_times_top1, dtype=float)
        tn = np.asarray(chosen_times_topn, dtype=float)
        return {
            "n_test_rows": n_test_rows,
            "top1_acc": hit1 / max(n_test_rows, 1),
            f"top{topn}_hit": hitn / max(n_test_rows, 1),
            "regret_cv_mean": float(np.nanmean(np.asarray(regrets_cv, dtype=float))),
            "regret_obj_mean": float(np.nanmean(np.asarray(regrets_obj, dtype=float))),
            "time_full_mean": float(np.nanmean(ft)),
            "time_top1_mean": float(np.nanmean(t1)),
            f"time_top{topn}_mean": float(np.nanmean(tn)),
        }

    for fold, (tr, te) in enumerate(gkf.split(all_idx, y=[0] * len(all_idx), groups=groups), start=1):
        tr_idx = [all_idx[i] for i in tr]
        te_idx = [all_idx[i] for i in te]
        r = eval_one_split(tr_idx, te_idx)
        r["fold"] = fold
        fold_results.append(r)

    top1s = [r["top1_acc"] for r in fold_results]
    topns = [r[f"top{topn}_hit"] for r in fold_results]
    full_mean = float(np.nanmean([r["time_full_mean"] for r in fold_results]))
    top1_mean = float(np.nanmean([r["time_top1_mean"] for r in fold_results]))
    topn_mean = float(np.nanmean([r[f"time_top{topn}_mean"] for r in fold_results]))

    return {
        "experience": str(Path(experience).resolve()),
        "label_target": label_target,
        "w_utility": float(w_utility),
        "w_stability": float(w_stability),
        "w_cost": float(w_cost),
        "aggregate_by_dataset": bool(aggregate_by_dataset),
        "cv_datasets": int(cv_datasets),
        "datasets_total": int(len(unique_groups)),
        "rows_total": int(len(all_idx)),
        "top1_acc_mean": float(np.mean(top1s)),
        f"top{topn}_hit_mean": float(np.mean(topns)),
        "top1_per_fold": [float(x) for x in top1s],
        f"top{topn}_per_fold": [float(x) for x in topns],
        "regret_cv_mean": float(np.nanmean([r["regret_cv_mean"] for r in fold_results])),
        "regret_obj_mean": float(np.nanmean([r["regret_obj_mean"] for r in fold_results])),
        "time_full_mean": float(full_mean),
        "time_top1_mean": float(top1_mean),
        f"time_top{topn}_mean": float(topn_mean),
        "time_top1_ratio": float(top1_mean / full_mean) if full_mean else float("nan"),
        f"time_top{topn}_ratio": float(topn_mean / full_mean) if full_mean else float("nan"),
    }


def format_report(r: Dict[str, Any], *, topn: int) -> str:
    topn = int(topn)
    lines = []
    lines.append("=== Meta Selector Evaluation (dataset-wise split) ===")
    lines.append(f"experience : {r['experience']}")
    lines.append(f"label      : {r['label_target']} (w_u={r['w_utility']}, w_s={r['w_stability']}, w_c={r['w_cost']})")
    lines.append(f"split      : GroupKFold over dataset_id (k={r['cv_datasets']})")
    lines.append(f"datasets   : total={r['datasets_total']}")
    lines.append(f"rows       : total={r['rows_total']}")
    lines.append(f"aggregate  : {r['aggregate_by_dataset']}")
    lines.append("")
    lines.append(f"top1_acc_mean : {r['top1_acc_mean']:.3f}   (per-fold: {[round(x,3) for x in r['top1_per_fold']]})")
    lines.append(f"top{topn}_hit_mean : {r[f'top{topn}_hit_mean']:.3f}   (per-fold: {[round(x,3) for x in r[f'top{topn}_per_fold']]})")
    lines.append("")
    lines.append(f"regret_cv_mean    : {r['regret_cv_mean']:.6f}")
    lines.append(f"regret_obj_mean   : {r['regret_obj_mean']:.6f}")
    lines.append("")
    lines.append(f"time_full_mean    : {r['time_full_mean']:.6f} sec")
    lines.append(f"time_top1_mean    : {r['time_top1_mean']:.6f} sec   (ratio={r['time_top1_ratio']:.3f})")
    lines.append(f"time_top{topn}_mean  : {r[f'time_top{topn}_mean']:.6f} sec   (ratio={r[f'time_top{topn}_ratio']:.3f})")
    return "\n".join(lines) + "\n"


def cli_main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Evaluate MTBMT meta selector (dataset-wise split) with regret/time metrics.")
    ap.add_argument("--experience", default="experience/experience.jsonl", help="Experience JSONL path")
    ap.add_argument("--seed", type=int, default=0, help="Random seed for splitting/training")
    ap.add_argument("--cv-datasets", type=int, default=5, help="GroupKFold folds over dataset_id")
    ap.add_argument("--rf-estimators", type=int, default=300, help="RandomForest n_estimators")
    ap.add_argument("--label-target", choices=["objective", "cv"], default="objective")
    ap.add_argument("--w-utility", type=float, default=1.0)
    ap.add_argument("--w-stability", type=float, default=0.10)
    ap.add_argument("--w-cost", type=float, default=0.15)
    ap.add_argument("--topn", type=int, default=2)
    ap.add_argument("--aggregate-by-dataset", action="store_true")
    args = ap.parse_args(argv)

    r = evaluate_meta_selector(
        experience=args.experience,
        seed=args.seed,
        cv_datasets=args.cv_datasets,
        rf_estimators=args.rf_estimators,
        label_target=args.label_target,
        w_utility=args.w_utility,
        w_stability=args.w_stability,
        w_cost=args.w_cost,
        topn=args.topn,
        aggregate_by_dataset=bool(args.aggregate_by_dataset),
    )
    print(format_report(r, topn=int(args.topn)), end="")
    return 0


