from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import LabelEncoder, OneHotEncoder
from sklearn.tree import DecisionTreeClassifier

from mtbmt.guided_cart import GuidedCARTClassifier
from mtbmt.hpo.guided_asha import HpoReranker, collect_promotion_training_data, run_asha, run_guided_asha
from mtbmt.meta_features import compute_dataset_meta_features, infer_task
from mtbmt.meta_learner import predict_top_methods, train_meta_selector
from mtbmt.relevance.base import BaseRelevanceScorer
from mtbmt.relevance.filters import DistanceCorrelationScorer, MutualInfoScorer, PearsonAbsScorer, SpearmanAbsScorer
from mtbmt.trajectory_guidance import TrajectoryRouteParams, guide_decision_tree_training


@dataclass(frozen=True)
class FeatureSelectionResult:
    method: str
    topk_idx: List[int]
    topk_names: List[str]


def _build_preprocessor(X_df: pd.DataFrame) -> ColumnTransformer:
    cat_cols = [
        c
        for c in X_df.columns
        if (X_df[c].dtype == "object" or str(X_df[c].dtype).startswith("category") or str(X_df[c].dtype) == "bool")
    ]
    num_cols = [c for c in X_df.columns if c not in cat_cols]

    cat_pipe = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore")),
        ]
    )
    num_pipe = Pipeline(steps=[("impute", SimpleImputer(strategy="median"))])

    return ColumnTransformer(
        transformers=[
            ("cat", cat_pipe, cat_cols),
            ("num", num_pipe, num_cols),
        ],
        remainder="drop",
    )


def _to_dense(X):
    try:
        return X.toarray()
    except Exception:
        return X


def _metric_for_classification(y: np.ndarray) -> Tuple[str, bool]:
    approx_task, y_unique, _ = infer_task(y)
    if approx_task != "classification":
        raise SystemExit("该脚本当前只支持分类任务（classification）。")
    is_binary = int(y_unique) == 2
    return ("roc_auc" if is_binary else "accuracy"), bool(is_binary)


def _safe_train_test_split(
    X,
    y: np.ndarray,
    *,
    test_size: float,
    random_state: int,
    stratify: bool,
    name: str,
):
    """
    Stratified split can fail when some classes have <2 samples or split sizes are too small.
    In that case we fall back to non-stratified split to keep large batch evaluations running.
    """
    y = np.asarray(y)
    if not bool(stratify):
        return train_test_split(X, y, test_size=float(test_size), random_state=int(random_state), stratify=None)

    # Pre-check: stratify requires each class to have at least 2 samples in the overall data.
    try:
        u, c = np.unique(y, return_counts=True)
        rare = u[c < 2]
        if rare.size > 0:
            sys.stderr.write(
                f"[warn] {name}: 存在样本数<2的类别（示例：{list(rare[:10])}），无法分层划分，已自动改为不分层。\n"
            )
            sys.stderr.flush()
            return train_test_split(X, y, test_size=float(test_size), random_state=int(random_state), stratify=None)
    except Exception:
        # If anything goes wrong, fall back to trying stratify and catching errors.
        pass

    try:
        return train_test_split(X, y, test_size=float(test_size), random_state=int(random_state), stratify=y)
    except ValueError as e:
        # Common causes: class counts too small for the requested split sizes.
        sys.stderr.write(f"[warn] {name}: stratify 失败，已自动改为不分层。错误：{e}\n")
        sys.stderr.flush()
        return train_test_split(X, y, test_size=float(test_size), random_state=int(random_state), stratify=None)


def _drop_rare_classes(X_df: pd.DataFrame, y: np.ndarray, *, min_count: int = 2) -> Tuple[pd.DataFrame, np.ndarray]:
    """
    Drop classes with too few samples. This avoids:
    - Stratified split failures (needs >=2 per class)
    - LabelEncoder.transform failures if a class appears only in val/test
    """
    y_arr = np.asarray(y)

    # Only meaningful for classification-like labels.
    # For regression/continuous targets, most values are unique (count=1) and dropping them would wipe the dataset.
    approx_task, y_unique, _ = infer_task(y_arr)
    if approx_task != "classification":
        return X_df, y_arr

    u, c = np.unique(y_arr, return_counts=True)
    rare = set(u[c < int(min_count)].tolist())
    if not rare:
        return X_df, y_arr
    mask = np.array([yy not in rare for yy in y_arr], dtype=bool)
    dropped = int((~mask).sum())
    sys.stderr.write(
        f"[warn] drop_rare_classes: 丢弃样本数<{int(min_count)}的类别，共删除 {dropped} 行；"
        f"稀有类别示例：{list(sorted(rare))[:10]}\n"
    )
    sys.stderr.flush()
    return X_df.loc[mask].reset_index(drop=True), y_arr[mask]


def _score_cls(y_true: np.ndarray, y_pred: np.ndarray, y_proba_1: Optional[np.ndarray], *, metric: str) -> float:
    if metric == "roc_auc":
        assert y_proba_1 is not None
        return float(roc_auc_score(y_true, y_proba_1))
    return float((y_true == y_pred).mean())


@dataclass
class _Progress:
    enabled: bool = True
    last_len: int = 0
    start: float = 0.0

    def begin(self) -> None:
        self.start = perf_counter()

    def update(self, msg: str) -> None:
        if not self.enabled:
            return
        line = msg.replace("\n", " ")
        pad = max(self.last_len - len(line), 0)
        sys.stderr.write("\r" + line + (" " * pad))
        sys.stderr.flush()
        self.last_len = len(line)

    def done(self) -> None:
        if not self.enabled:
            return
        sys.stderr.write("\n")
        sys.stderr.flush()
        self.last_len = 0


def _fmt_eta(done: int, total: int, start_t: float) -> str:
    done = int(done)
    total = int(total)
    if done <= 0 or total <= 0:
        return "eta=?"
    elapsed = perf_counter() - float(start_t)
    rate = elapsed / max(1, done)
    eta = rate * max(0, total - done)
    return f"elapsed={elapsed:.1f}s eta={eta:.1f}s"


def _scorer_from_name(name: str, *, task: str) -> BaseRelevanceScorer:
    n = (name or "").strip().lower()
    # names aligned with mtbmt.meta_learner normalization
    if n in {"pearson", "pearson_abs"}:
        return PearsonAbsScorer()
    if n in {"spearman", "spearman_abs"}:
        return SpearmanAbsScorer()
    if n in {"mutual_info", "mi"}:
        return MutualInfoScorer(task=task, n_neighbors=3, random_state=0)
    if n in {"distance_correlation", "dcor"}:
        return DistanceCorrelationScorer()
    # fallback: mutual_info is usually a reasonable default
    return MutualInfoScorer(task=task, n_neighbors=3, random_state=0)


def _select_features(
    *,
    X_train: np.ndarray,
    y_train: np.ndarray,
    feature_names: List[str],
    experience_jsonl: str,
    top_k: int,
) -> FeatureSelectionResult:
    # 1) meta-selector recommends which relevance method family to use
    _metric, _is_binary = _metric_for_classification(y_train)
    meta = compute_dataset_meta_features(X_train, y_train).as_dict()
    cand_methods: List[str] = []
    exp_path = str(experience_jsonl or "").strip()
    if exp_path and Path(exp_path).exists():
        try:
            bundle = train_meta_selector(exp_path, aggregate_by_dataset=True)
            top = predict_top_methods(bundle, meta_features=meta, trajectory_features=None, top_n=5)
            cand_methods = [m for (m, _p) in top] or []
        except Exception:
            # If experience is empty/unreadable, fall back to a stable default.
            cand_methods = []

    # pick first supported method family; fall back to mutual_info
    supported = {"pearson_abs", "spearman_abs", "mutual_info", "distance_correlation", "pearson", "spearman", "mi", "dcor"}
    chosen = None
    for m in cand_methods:
        key = (m or "").strip().lower()
        if key in supported or key.startswith("mutual_info"):
            chosen = m
            break
    if chosen is None:
        chosen = "mutual_info"
    scorer = _scorer_from_name(chosen, task="classification")

    # 2) run relevance scoring on train, choose top-k
    rel = scorer.fit_score(X_train, y_train, feature_names=feature_names)
    idx = np.argsort(-np.asarray(rel.scores, dtype=float))[: int(top_k)].tolist()
    names = [feature_names[i] for i in idx]
    return FeatureSelectionResult(method=scorer.name, topk_idx=idx, topk_names=names)


def _make_gbdt_objective(
    *,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    seed: int,
    metric: str,
) -> Any:
    X_train = np.asarray(X_train, dtype=float)
    X_val = np.asarray(X_val, dtype=float)
    y_train = np.asarray(y_train)
    y_val = np.asarray(y_val)

    def _auc(p1: np.ndarray) -> float:
        return float(roc_auc_score(y_val, p1))

    def objective(cfg, resource: int, _seed: int) -> Tuple[float, Sequence[float]]:
        p = cfg.params
        lr = float(p.get("learning_rate", 0.1))
        subsample = float(p.get("subsample", 1.0))
        max_depth = int(round(float(p.get("max_depth", 3.0))))
        min_samples_leaf = int(round(float(p.get("min_samples_leaf", 1.0))))

        max_depth = max(1, min(max_depth, 8))
        min_samples_leaf = max(1, min(min_samples_leaf, 200))
        subsample = max(0.2, min(subsample, 1.0))
        lr = max(1e-3, min(lr, 0.5))

        clf = GradientBoostingClassifier(
            n_estimators=max(1, int(resource)),
            learning_rate=lr,
            subsample=subsample,
            max_depth=max_depth,
            min_samples_leaf=min_samples_leaf,
            random_state=int(seed),
        )
        clf.fit(X_train, y_train)

        traj: List[float] = []
        if metric == "roc_auc":
            for proba in clf.staged_predict_proba(X_val):
                traj.append(_auc(proba[:, 1]))
        else:
            for pred in clf.staged_predict(X_val):
                traj.append(float((pred == y_val).mean()))
        return float(traj[-1]) if traj else float("nan"), traj

    return objective


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Run all guided variants with the recommended combo: feature selection -> (Guided-CART / Trajectory Guidance / Guided-ASHA)."
    )
    ap.add_argument("--csv", default="", help="CSV path (alternative to OpenML).")
    ap.add_argument("--target", default="", help="Target column (required for --csv).")
    ap.add_argument("--openml-id", type=int, default=0, help="OpenML dataset id (alternative to --csv).")
    ap.add_argument("--data-home", type=str, default="data/openml_cache", help="Cache dir for OpenML downloads.")

    ap.add_argument(
        "--experience",
        default="",
        help="Experience JSONL path (optional). If provided and readable, meta-selector will recommend a relevance method; otherwise falls back to a default method.",
    )
    ap.add_argument("--top-k", type=int, default=20, help="Top-k features kept after relevance scoring.")

    ap.add_argument("--seed0", type=int, default=42)
    ap.add_argument("--test-size", type=float, default=0.25)
    ap.add_argument("--val-size", type=float, default=0.25, help="Fraction of (train) used as validation.")

    # Guided-CART options
    ap.add_argument("--cart-depth", type=int, default=8)
    ap.add_argument("--guided-cart-mode", choices=["rerank", "replace"], default="rerank")
    ap.add_argument("--guided-cart-alpha", type=float, default=1.0)
    ap.add_argument("--guided-cart-shortlist-k", type=int, default=8)
    ap.add_argument("--guided-cart-thresholds", type=int, default=32)
    ap.add_argument("--guided-cart-threshold-strategy", choices=["quantile", "unique_midpoints_cap", "unique_midpoints"], default="unique_midpoints_cap")
    ap.add_argument("--guided-cart-reranker-target", choices=["gini", "val_gain"], default="val_gain")

    # Trajectory Guidance options
    ap.add_argument("--tg-iters", type=int, default=6)
    ap.add_argument("--tg-traj-samples", type=int, default=512)

    # Guided-ASHA options (GBDT)
    ap.add_argument("--hpo-train-seeds", type=int, default=4)
    ap.add_argument("--hpo-test-seeds", type=int, default=4)
    ap.add_argument("--n0", type=int, default=18)
    ap.add_argument("--eta", type=int, default=3)
    ap.add_argument("--min-resource", type=int, default=20)
    ap.add_argument("--max-resource", type=int, default=200)
    ap.add_argument("--hpo-mode", choices=["rerank", "replace"], default="rerank")
    ap.add_argument("--hpo-shortlist-k", type=int, default=9)
    ap.add_argument("--hpo-alpha", type=float, default=0.25)
    ap.add_argument("--pos-top-frac", type=float, default=0.35)
    ap.add_argument("--label-kind", choices=["uplift", "next_score"], default="uplift")
    ap.add_argument("--out-json", default="", help="Optional: write a JSON report to this path.")
    ap.add_argument("--no-stratify", action="store_true", help="Disable stratified train/val/test split.")
    ap.add_argument(
        "--keep-rare-classes",
        action="store_true",
        help="Keep classes with <2 samples (may break stratify and can cause unseen-label issues).",
    )

    args = ap.parse_args(argv)

    if not args.csv and int(args.openml_id) <= 0:
        raise SystemExit("请提供 --csv/--target 或 --openml-id。")

    # --- load dataset ---
    if args.csv:
        if not args.target:
            raise SystemExit("使用 --csv 时必须提供 --target。")
        df = pd.read_csv(args.csv)
        if args.target not in df.columns:
            raise SystemExit(f"target列不存在：{args.target}")
        y_raw = df[args.target].to_numpy()
        X_df = df.drop(columns=[args.target])
    else:
        try:
            from sklearn.datasets import fetch_openml
        except Exception as e:
            raise SystemExit(f"无法导入 fetch_openml：{e}")
        bunch = fetch_openml(data_id=int(args.openml_id), as_frame=True, data_home=str(args.data_home))
        X_df = bunch.data  # type: ignore[assignment]
        y_ser = bunch.target  # type: ignore[attr-defined]
        if isinstance(y_ser, pd.DataFrame):
            if y_ser.shape[1] != 1:
                raise SystemExit(f"OpenML target has {y_ser.shape[1]} columns; please choose a single target.")
            y_ser = y_ser.iloc[:, 0]
        y_raw = y_ser.to_numpy()

    # Quick task gate before any "class dropping" logic
    approx_task0, _, _ = infer_task(np.asarray(y_raw))
    if approx_task0 != "classification":
        raise SystemExit("该脚本当前只支持分类任务（classification）。")

    # --- split & preprocess ---
    if not bool(args.keep_rare_classes):
        # Ensure downstream split/encoding won't crash on ultra-rare classes.
        X_df, y_raw = _drop_rare_classes(X_df, np.asarray(y_raw), min_count=2)
    if int(len(X_df)) < 10:
        raise SystemExit(f"可用样本过少（n={len(X_df)}），跳过该数据集。")
    X_train_df, X_test_df, y_train_raw, y_test_raw = _safe_train_test_split(
        X_df,
        np.asarray(y_raw),
        test_size=float(args.test_size),
        random_state=int(args.seed0),
        stratify=(not bool(args.no_stratify)),
        name="train/test",
    )
    X_tr_df, X_val_df, y_tr_raw, y_val_raw = _safe_train_test_split(
        X_train_df,
        np.asarray(y_train_raw),
        test_size=float(args.val_size),
        random_state=int(args.seed0),
        stratify=(not bool(args.no_stratify)),
        name="train/val",
    )

    pre = _build_preprocessor(X_tr_df)
    X_tr = _to_dense(pre.fit_transform(X_tr_df))
    X_val = _to_dense(pre.transform(X_val_df))
    X_test = _to_dense(pre.transform(X_test_df))

    # feature names after onehot: we approximate with numeric indices (stable mapping is not essential for top-k indices)
    feature_names = [f"x{i}" for i in range(int(X_tr.shape[1]))]

    # label encoding for classification
    metric, is_binary = _metric_for_classification(np.asarray(y_tr_raw))
    le = LabelEncoder()
    y_tr = le.fit_transform(np.asarray(y_tr_raw))
    y_val = le.transform(np.asarray(y_val_raw))
    y_test = le.transform(np.asarray(y_test_raw))

    # --- 0) recommended combo: feature selection FIRST ---
    t_fs0 = perf_counter()
    fs = _select_features(
        X_train=np.asarray(X_tr, dtype=float),
        y_train=np.asarray(y_tr),
        feature_names=feature_names,
        experience_jsonl=str(args.experience),
        top_k=int(args.top_k),
    )
    dt_fs = perf_counter() - t_fs0
    X_tr_k = np.asarray(X_tr, dtype=float)[:, fs.topk_idx]
    X_val_k = np.asarray(X_val, dtype=float)[:, fs.topk_idx]
    X_test_k = np.asarray(X_test, dtype=float)[:, fs.topk_idx]

    print("=== Guided Combo Eval ===")
    print("data:", ("csv" if args.csv else f"openml_id={int(args.openml_id)}"), "metric:", metric)
    print("shapes:", "X_tr", X_tr.shape, "->", X_tr_k.shape, "X_val", X_val_k.shape, "X_test", X_test_k.shape)
    print(f"feature_selection: method={fs.method} top_k={len(fs.topk_idx)} time_sec={dt_fs:.3f}")
    print()

    results: Dict[str, Any] = {"metric": metric, "feature_selection": {"method": fs.method, "top_k": len(fs.topk_idx)}}

    # --- 1) Guided-CART vs sklearn CART (on selected features) ---
    t0 = perf_counter()
    cart = DecisionTreeClassifier(max_depth=int(args.cart_depth), criterion="gini", random_state=int(args.seed0))
    cart.fit(X_tr_k, y_tr)
    pred_cart = cart.predict(X_test_k)
    acc_cart = float((pred_cart == y_test).mean())
    proba_cart = cart.predict_proba(X_test_k)[:, 1] if metric == "roc_auc" else None
    score_cart = _score_cls(y_test, pred_cart, proba_cart, metric=metric)
    dt_cart = perf_counter() - t0

    t1 = perf_counter()
    gcart = GuidedCARTClassifier(
        max_depth=int(args.cart_depth),
        mode=str(args.guided_cart_mode),
        shortlist_k=int(args.guided_cart_shortlist_k),
        alpha=float(args.guided_cart_alpha),
        max_thresholds_per_feature=int(args.guided_cart_thresholds),
        threshold_strategy=str(args.guided_cart_threshold_strategy),
        reranker_target=str(args.guided_cart_reranker_target),
        val_fraction=0.25,
        val_seed=int(args.seed0),
    )
    gcart.fit(X_tr_k, y_tr)
    pred_gcart = gcart.predict(X_test_k)
    score_gcart = _score_cls(y_test, pred_gcart, None, metric="accuracy")  # guided_cart only outputs labels; use acc
    agree = float((pred_cart == pred_gcart).mean())
    dt_gcart = perf_counter() - t1

    results["guided_cart"] = {
        "sklearn_cart": {"metric_score": score_cart, "acc": acc_cart, "time_sec": dt_cart},
        "guided_cart": {
            "acc": score_gcart,
            "delta_acc": float(score_gcart - acc_cart),
            "time_sec": dt_gcart,
            "agreement_vs_sklearn": agree,
            "tree_stats": gcart.tree_stats(),
        },
    }

    # --- 2) Trajectory Guidance (hyperparam correction) ---
    # baseline for comparison: a plain DecisionTree with the same initial params we feed into the guider
    t2b = perf_counter()
    base_dt = DecisionTreeClassifier(max_depth=int(args.cart_depth), min_samples_leaf=2, min_samples_split=5, random_state=int(args.seed0))
    base_dt.fit(X_tr_k, y_tr)
    base_dt_acc = float(base_dt.score(X_test_k, y_test))
    dt_base_dt = perf_counter() - t2b

    t2 = perf_counter()
    route = TrajectoryRouteParams(w_effect=1.0, w_length=0.05, w_time=0.10, n_samples_for_trajectory=int(args.tg_traj_samples), max_iters=int(args.tg_iters))
    clf_tg, params_tg, report_tg = guide_decision_tree_training(
        X_train=X_tr_k,
        y_train=y_tr,
        X_val=X_val_k,
        y_val=y_val,
        X_test=X_test_k,
        y_test=y_test,
        route=route,
        initial_params={"max_depth": int(args.cart_depth), "min_samples_leaf": 2, "min_samples_split": 5},
        random_state=int(args.seed0),
    )
    dt_tg = perf_counter() - t2
    results["trajectory_guidance"] = {
        "baseline_dt": {"acc": float(base_dt_acc), "time_sec": float(dt_base_dt)},
        "guided": {
            "acc": float(report_tg["decision_effect"]),
            "delta_acc": float(float(report_tg["decision_effect"]) - float(base_dt_acc)),
            "objective": float(report_tg["objective"]),
            "time_sec": float(dt_tg),
            "params": params_tg,
        },
    }

    # --- 3) Guided-ASHA vs ASHA on GBDT (on selected features) ---
    # HPO runs on train/val only; report best_at_resource(R) on val metric.
    search_space: Dict[str, Tuple[float, float, str]] = {
        "learning_rate": (0.01, 0.3, "log"),
        "subsample": (0.5, 1.0, "uniform"),
        "max_depth": (1.0, 6.0, "uniform"),
        "min_samples_leaf": (1.0, 60.0, "log"),
    }
    train_seeds = [int(args.seed0) + i for i in range(int(args.hpo_train_seeds))]
    test_seeds = [int(args.seed0) + int(args.hpo_train_seeds) + i for i in range(int(args.hpo_test_seeds))]

    t3 = perf_counter()
    X_rows_all: List[dict] = []
    y_rows_all: List[int] = []
    prog = _Progress(enabled=True)
    prog.begin()
    for i, s in enumerate(train_seeds, start=1):
        prog.update(f"[hpo-train] seed {i}/{len(train_seeds)}  {_fmt_eta(i-1, len(train_seeds), prog.start)}")
        obj = _make_gbdt_objective(X_train=X_tr_k, y_train=y_tr, X_val=X_val_k, y_val=y_val, seed=int(s), metric=metric)
        X_rows, y_rows = collect_promotion_training_data(
            objective=obj,
            search_space=search_space,
            X=X_tr_k,
            y=y_tr,
            seed=int(s),
            n0=int(args.n0),
            eta=int(args.eta),
            min_resource=int(args.min_resource),
            max_resource=int(args.max_resource),
            positive_top_frac=float(args.pos_top_frac),
            label_kind=str(args.label_kind),
        )
        X_rows_all.extend(X_rows)
        y_rows_all.extend(y_rows)
    prog.done()
    reranker = HpoReranker(random_state=int(args.seed0))
    reranker.fit(X_rows_all, y_rows_all)
    dt_reranker = perf_counter() - t3

    base_finals: List[float] = []
    guided_finals: List[float] = []
    disagree: List[float] = []
    t4 = perf_counter()
    prog = _Progress(enabled=True)
    prog.begin()
    for i, s in enumerate(test_seeds, start=1):
        prog.update(f"[hpo-eval] seed {i}/{len(test_seeds)}  {_fmt_eta(i-1, len(test_seeds), prog.start)}")
        obj = _make_gbdt_objective(X_train=X_tr_k, y_train=y_tr, X_val=X_val_k, y_val=y_val, seed=int(s), metric=metric)
        base = run_asha(
            objective=obj,
            search_space=search_space,
            seed=int(s),
            n0=int(args.n0),
            eta=int(args.eta),
            min_resource=int(args.min_resource),
            max_resource=int(args.max_resource),
        )
        guided = run_guided_asha(
            objective=obj,
            search_space=search_space,
            X=X_tr_k,
            y=y_tr,
            seed=int(s),
            reranker=reranker,
            n0=int(args.n0),
            eta=int(args.eta),
            min_resource=int(args.min_resource),
            max_resource=int(args.max_resource),
            mode=str(args.hpo_mode),
            shortlist_k=int(args.hpo_shortlist_k),
            alpha=float(args.hpo_alpha),
        )
        b = base.best_at_resource(int(args.max_resource))
        g = guided.best_at_resource(int(args.max_resource))
        base_finals.append(float(b.score) if b is not None else float("nan"))
        guided_finals.append(float(g.score) if g is not None else float("nan"))
        disagree.append(float(guided.promote_disagree_rate))
    prog.done()
    dt_hpo = perf_counter() - t4

    def _mean_std(xs: Sequence[float]) -> Tuple[float, float]:
        a = np.asarray(xs, dtype=float)
        return float(np.nanmean(a)), float(np.nanstd(a))

    base_m, base_s = _mean_std(base_finals)
    guid_m, guid_s = _mean_std(guided_finals)
    dis_m, dis_s = _mean_std(disagree)
    results["guided_asha"] = {
        "label_kind": str(args.label_kind),
        "train_reranker_time_sec": float(dt_reranker),
        "eval_time_sec": float(dt_hpo),
        "final_R_base_mean": base_m,
        "final_R_base_std": base_s,
        "final_R_guided_mean": guid_m,
        "final_R_guided_std": guid_s,
        "delta_final_mean": float(guid_m - base_m),
        "promote_disagree_mean": dis_m,
        "promote_disagree_std": dis_s,
    }

    # --- print summary table ---
    print("=== Results (same split, after feature selection) ===")
    print(
        f"[Guided-CART] sklearn_acc={acc_cart:.4f} sklearn_{metric}={score_cart:.4f}  "
        f"guided_acc={score_gcart:.4f} delta_acc={score_gcart-acc_cart:+.4f}  agree={agree:.3f}"
    )
    print(
        f"[TrajectoryGuidance] baseline_acc={base_dt_acc:.4f} guided_acc={report_tg['decision_effect']:.4f} "
        f"delta_acc={float(report_tg['decision_effect'])-base_dt_acc:+.4f} obj={report_tg['objective']:.4f} params={params_tg}"
    )
    print(
        "[Guided-ASHA] final@R(base)={:.4f}+/-{:.4f}  final@R(guided)={:.4f}+/-{:.4f}  delta={:+.4f}  disagree={:.3f}+/-{:.3f}".format(
            base_m, base_s, guid_m, guid_s, guid_m - base_m, dis_m, dis_s
        )
    )

    if str(getattr(args, "out_json", "") or "").strip():
        with open(str(args.out_json), "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


