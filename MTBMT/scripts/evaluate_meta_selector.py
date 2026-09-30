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
from sklearn.model_selection import GroupKFold
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder
from sklearn.impute import SimpleImputer


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
    """
    返回：
    - X_rows: list[dict]  扁平化后的元特征（供 sklearn 的 dict pipeline 使用）
    - y: list[str]        目标标签：每条记录的“最优方法”
    - meta: list[dict]    每条记录的附加信息（dataset_id、各方法指标、时间等），用于评估
    """
    raw_rows: List[Dict[str, Any]] = []
    raw_meta: List[Dict[str, Any]] = []

    for obj in _iter_experience(experience_path):
        dataset_id = str(obj.get("dataset_id", "") or "")
        mf = obj.get("meta_features", {}) or {}
        tf = obj.get("trajectory_features", None)
        evaluations = obj.get("evaluations", {}) or {}

        # 归一化 evaluations 的 key & method_name
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
        # 保存每条 record 的“评估明细”，用于后续聚合与计算 label/指标
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
            times = m["times"]
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

    # --- aggregate by dataset_id ---
    by_ds: DefaultDict[str, List[int]] = defaultdict(list)
    for i, m in enumerate(raw_meta):
        ds = str(m.get("dataset_id", "") or "")
        if ds:
            by_ds[ds].append(i)

    X_rows_agg: List[Dict[str, Any]] = []
    y_agg: List[str] = []
    meta_agg: List[Dict[str, Any]] = []

    for ds, idxs in by_ds.items():
        # 聚合 meta_features/trajectory_features：数值取均值；非数值取出现最多或第一条（简单策略）
        # 这里先保证稳定：对数值列取均值，字符串取第一条。
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

        # 聚合 evaluations：对每个 method 的 cv/obj/time 分别取均值（只在该条记录存在时纳入）
        methods = sorted({mm for i in idxs for mm in raw_meta[i]["methods"]})
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

        if len(methods) < 2:
            continue

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
                # “full_time” 用聚合后的 per-method time 求和（与 Python 逻辑一致）
                "full_time": float(sum(max(float(t), 0.0) for t in times.values())),
            }
        )
        X_rows_agg.append(row_out)
        y_agg.append(best)

    return X_rows_agg, y_agg, meta_agg


def make_model(rows: List[Dict[str, Any]], *, random_state: int, n_estimators: int) -> Tuple[Pipeline, List[str]]:
    all_cols = sorted({k for r in rows for k in r.keys()})
    # 用 DataFrame 承载，避免 sklearn 将 list[dict] 视为 1D object array
    X_df = pd.DataFrame([{c: r.get(c, None) for c in all_cols} for r in rows], columns=all_cols)

    cat_cols = [c for c in all_cols if X_df[c].map(lambda v: isinstance(v, str)).any()]
    num_cols = [c for c in all_cols if c not in cat_cols]

    # 经验库聚合/拼接后会出现缺失值；RandomForest 不接受 NaN，因此这里显式做缺失填充。
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate MTBMT meta selector (dataset-wise split) with regret/time metrics.")
    ap.add_argument("--experience", default="experience/experience.jsonl", help="Experience JSONL path")
    ap.add_argument("--seed", type=int, default=0, help="Random seed for splitting/training")
    ap.add_argument("--test-frac", type=float, default=0.33, help="Fraction of dataset_ids used for test")
    ap.add_argument(
        "--cv-datasets",
        type=int,
        default=0,
        help="If >1, run GroupKFold cross-validation over dataset_id with this many folds (overrides --test-frac).",
    )
    ap.add_argument("--rf-estimators", type=int, default=300, help="RandomForest n_estimators")
    ap.add_argument(
        "--label-target",
        choices=["objective", "cv"],
        default="objective",
        help='Label definition: "objective" -> argmax J(A); "cv" -> argmax cv_score_mean',
    )
    ap.add_argument(
        "--aggregate-by-dataset",
        action="store_true",
        help="Aggregate multiple records of the same dataset_id into a single training/eval row (improves label consistency for Top1).",
    )
    ap.add_argument("--w-utility", type=float, default=1.0, help="w_u in J(A)")
    ap.add_argument("--w-stability", type=float, default=0.10, help="w_s in J(A)")
    ap.add_argument("--w-cost", type=float, default=0.15, help="w_c in J(A)")
    ap.add_argument("--topn", type=int, default=2, help="Report Top-N coverage (default: 2)")
    args = ap.parse_args()

    X_rows, y, meta = build_dataset(
        args.experience,
        w_utility=args.w_utility,
        w_stability=args.w_stability,
        w_cost=args.w_cost,
        label_target=args.label_target,
        aggregate_by_dataset=bool(args.aggregate_by_dataset),
    )
    if not X_rows:
        print(f"经验库为空或无法解析：{args.experience}")
        return 2

    # dataset-wise evaluation (either single split or GroupKFold)
    dataset_ids = sorted({m["dataset_id"] for m in meta if m.get("dataset_id")})
    if len(dataset_ids) < 3:
        print(f"可用 dataset_id 太少（{len(dataset_ids)} 个）：{dataset_ids}")
        print("元学习的泛化评估需要多个不同 dataset_id。建议先收集更多数据集的经验。")
        return 2

    topn = max(int(args.topn), 1)

    def eval_one_split(train_idx: List[int], test_idx: List[int]) -> Dict[str, Any]:
        model, feature_cols = make_model([X_rows[i] for i in train_idx], random_state=int(args.seed), n_estimators=int(args.rf_estimators))
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
            "regret_cv_median": float(np.nanmedian(np.asarray(regrets_cv, dtype=float))),
            "regret_obj_mean": float(np.nanmean(np.asarray(regrets_obj, dtype=float))),
            "regret_obj_median": float(np.nanmedian(np.asarray(regrets_obj, dtype=float))),
            "time_full_mean": float(np.nanmean(ft)),
            "time_top1_mean": float(np.nanmean(t1)),
            f"time_top{topn}_mean": float(np.nanmean(tn)),
        }

    # Build group vector for each row (dataset_id)
    groups = [m.get("dataset_id", "") for m in meta]
    all_idx = list(range(len(meta)))
    unique_groups = sorted(set(groups))

    fold_results: List[Dict[str, Any]] = []
    if int(args.cv_datasets) and int(args.cv_datasets) > 1:
        k = int(args.cv_datasets)
        if k > len(unique_groups):
            print(f"--cv-datasets={k} 但可用 dataset_id 只有 {len(unique_groups)} 个，无法做分组 K 折。")
            return 2
        gkf = GroupKFold(n_splits=k)
        for fold, (tr, te) in enumerate(gkf.split(all_idx, y=[0] * len(all_idx), groups=groups), start=1):
            tr_idx = [all_idx[i] for i in tr]
            te_idx = [all_idx[i] for i in te]
            r = eval_one_split(tr_idx, te_idx)
            r["fold"] = fold
            r["n_train_rows"] = len(tr_idx)
            r["n_test_rows"] = len(te_idx)
            r["n_train_datasets"] = len(set(groups[i] for i in tr_idx))
            r["n_test_datasets"] = len(set(groups[i] for i in te_idx))
            fold_results.append(r)
    else:
        rng = np.random.default_rng(int(args.seed))
        perm = unique_groups.copy()
        rng.shuffle(perm)
        n_test = max(1, int(round(float(args.test_frac) * len(perm))))
        test_ids = set(perm[:n_test])
        train_ids = set(perm[n_test:])
        train_idx = [i for i, m in enumerate(meta) if m["dataset_id"] in train_ids]
        test_idx = [i for i, m in enumerate(meta) if m["dataset_id"] in test_ids]
        if not train_idx or not test_idx:
            print("train/test 切分失败（某一侧为空）。请调整 --test-frac 或补充 dataset_id。")
            return 2
        r = eval_one_split(train_idx, test_idx)
        r["fold"] = 1
        r["n_train_rows"] = len(train_idx)
        r["n_test_rows"] = len(test_idx)
        r["n_train_datasets"] = len(train_ids)
        r["n_test_datasets"] = len(test_ids)
        r["test_dataset_ids"] = sorted(test_ids)
        fold_results.append(r)

    def _nanmean(a: List[float]) -> float:
        x = np.asarray(a, dtype=float)
        return float(np.nanmean(x))

    def _nanmedian(a: List[float]) -> float:
        x = np.asarray(a, dtype=float)
        return float(np.nanmedian(x))

    print("=== Meta Selector Evaluation (dataset-wise split) ===")
    print(f"experience : {Path(args.experience).resolve()}")
    print(f"label      : {args.label_target} (w_u={args.w_utility}, w_s={args.w_stability}, w_c={args.w_cost})")
    if int(args.cv_datasets) and int(args.cv_datasets) > 1:
        print(f"split      : GroupKFold over dataset_id (k={int(args.cv_datasets)})")
        print(f"datasets   : total={len(unique_groups)}")
        print(f"rows       : total={len(all_idx)}")
    else:
        print(f"split      : GroupShuffle (test_frac={args.test_frac}) over dataset_id")
        print(f"datasets   : total={len(unique_groups)} train={fold_results[0].get('n_train_datasets')} test={fold_results[0].get('n_test_datasets')}")
        print(f"rows       : train={fold_results[0].get('n_train_rows')} test={fold_results[0].get('n_test_rows')}")
    print()

    top1s = [r["top1_acc"] for r in fold_results]
    topns = [r[f"top{topn}_hit"] for r in fold_results]
    print(f"top1_acc_mean : {float(np.mean(top1s)):.3f}   (per-fold: {[round(x,3) for x in top1s]})")
    print(f"top{topn}_hit_mean : {float(np.mean(topns)):.3f}   (per-fold: {[round(x,3) for x in topns]})")
    print()

    print(f"regret_cv_mean    : {float(np.nanmean([r['regret_cv_mean'] for r in fold_results])):.6f}")
    print(f"regret_obj_mean   : {float(np.nanmean([r['regret_obj_mean'] for r in fold_results])):.6f}")
    print()
    full_mean = float(np.nanmean([r["time_full_mean"] for r in fold_results]))
    top1_mean = float(np.nanmean([r["time_top1_mean"] for r in fold_results]))
    topn_mean = float(np.nanmean([r[f"time_top{topn}_mean"] for r in fold_results]))
    print(f"time_full_mean    : {full_mean:.6f} sec")
    print(f"time_top1_mean    : {top1_mean:.6f} sec   (ratio={(top1_mean / full_mean) if full_mean else float('nan'):.3f})")
    print(f"time_top{topn}_mean  : {topn_mean:.6f} sec   (ratio={(topn_mean / full_mean) if full_mean else float('nan'):.3f})")
    if fold_results and "test_dataset_ids" in fold_results[0]:
        print()
        print(f"test_dataset_ids  : {fold_results[0]['test_dataset_ids']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


