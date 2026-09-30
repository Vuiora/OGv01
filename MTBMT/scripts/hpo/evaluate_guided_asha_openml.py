from __future__ import annotations

import argparse
from dataclasses import dataclass
import sys
from time import perf_counter
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.datasets import fetch_openml
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.impute import SimpleImputer
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder

from mtbmt.hpo.guided_asha import HpoConfig, HpoReranker, collect_promotion_training_data, run_asha, run_guided_asha
from mtbmt.meta_features import infer_task


@dataclass
class OneSeedResult:
    seed: int
    base_best: float
    base_final: float
    guided_best: float
    guided_final: float
    guided_disagree_rate: float


def _summary(xs: Sequence[float]) -> Tuple[float, float]:
    a = np.asarray(list(xs), dtype=float)
    return float(np.mean(a)), float(np.std(a))


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


def _parse_float_grid(s: str) -> List[float]:
    """
    Parse a comma-separated float grid, e.g. "0.1,0.2,0.25".
    Returns a sorted unique list (preserves numeric order).
    """
    s = (s or "").strip()
    if not s:
        return []
    out: List[float] = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    # stable unique + sorted
    out2 = sorted(set(out))
    return out2


def _alpha_arith_grid(*, start: float, end: float, step: float) -> List[float]:
    """
    Build an arithmetic progression grid: start, start+step, ... <= end (inclusive with eps).
    """
    start = float(start)
    end = float(end)
    step = float(step)
    if step <= 0:
        raise ValueError("--alpha-step must be > 0")
    if end < start:
        raise ValueError("--alpha-end must be >= --alpha-start")
    # inclusive end with tolerance
    eps = 1e-12
    xs: List[float] = []
    x = start
    # guard max points to avoid infinite loops due to tiny step
    max_points = 10_000
    for _ in range(max_points):
        if x > end + eps:
            break
        xs.append(float(x))
        x = x + step
    if not xs:
        return []
    # snap last value to end if it's extremely close
    if abs(xs[-1] - end) <= 1e-9:
        xs[-1] = float(end)
    return xs


def _build_preprocessor(X_df: pd.DataFrame) -> ColumnTransformer:
    # heuristics: object/category/bool -> categorical; rest -> numeric
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


def _make_gbdt_objective(
    *,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    seed: int,
) -> Any:
    """
    Objective:
    - model: GradientBoostingClassifier (GBDT)
    - resource: n_estimators
    - score: validation metric (roc_auc for binary, else accuracy)
    - trajectory: staged validation metric for each estimator
    """

    X_train = np.asarray(X_train, dtype=float)
    X_val = np.asarray(X_val, dtype=float)
    y_train = np.asarray(y_train)
    y_val = np.asarray(y_val)

    approx_task, y_unique, _ = infer_task(y_train)
    is_binary = (approx_task == "classification") and int(y_unique) == 2

    # ensure binary labels are 0/1 for AUC
    if is_binary and not np.issubdtype(y_train.dtype, np.number):
        vals = list(dict.fromkeys(list(y_train)))
        if len(vals) != 2:
            raise ValueError(f"binary classification expected, got labels={vals}")
        m = {vals[0]: 0, vals[1]: 1}
        y_train = np.asarray([m[v] for v in y_train], dtype=int)
        y_val = np.asarray([m[v] for v in y_val], dtype=int)

    def _auc(p1: np.ndarray) -> float:
        return float(roc_auc_score(y_val, p1))

    def objective(cfg: HpoConfig, resource: int, _seed: int) -> Tuple[float, Sequence[float]]:
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
        if is_binary:
            for proba in clf.staged_predict_proba(X_val):
                traj.append(_auc(proba[:, 1]))
        else:
            for pred in clf.staged_predict(X_val):
                traj.append(float((pred == y_val).mean()))
        return float(traj[-1]) if traj else float("nan"), traj

    return objective


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate baseline ASHA vs Guided-ASHA on a real dataset (OpenML).")
    ap.add_argument("--openml-id", type=int, default=0, help="OpenML dataset id (preferred).")
    ap.add_argument("--openml-name", type=str, default="", help="OpenML dataset name (alternative).")
    ap.add_argument("--data-home", type=str, default="data/openml_cache", help="Cache dir for OpenML downloads.")

    ap.add_argument("--train-seeds", type=int, default=6, help="How many HPO RNG seeds used to train the reranker.")
    ap.add_argument("--test-seeds", type=int, default=6, help="How many HPO RNG seeds used to evaluate guided vs baseline.")
    ap.add_argument("--seed0", type=int, default=42, help="Base seed.")

    ap.add_argument("--n0", type=int, default=18, help="Initial number of configs.")
    ap.add_argument("--eta", type=int, default=3)
    ap.add_argument("--min-resource", type=int, default=20, help="Min resource (GBDT n_estimators).")
    ap.add_argument("--max-resource", type=int, default=200, help="Max resource (GBDT n_estimators).")

    ap.add_argument("--mode", choices=["rerank", "replace"], default="rerank")
    ap.add_argument("--shortlist-k", type=int, default=9)
    ap.add_argument("--alpha", type=float, default=0.25)
    ap.add_argument("--max-swaps", type=int, default=-1, help="Max replacements vs baseline promotions per rung (-1 means k). Smaller -> less noisy.")
    ap.add_argument(
        "--min-mix-margin",
        type=float,
        default=0.0,
        help="Only swap-in if mix(best_extra) > mix(worst_promoted) + margin. Larger -> more conservative.",
    )
    ap.add_argument(
        "--min-proba",
        type=float,
        default=0.0,
        help="Only allow swap-in if reranker proba >= this threshold (e.g. 0.6).",
    )
    ap.add_argument(
        "--alpha-grid",
        type=str,
        default="",
        help='Comma-separated alpha values to sweep, e.g. "0.1,0.2,0.25,0.3". If set, overrides --alpha.',
    )
    ap.add_argument("--alpha-start", type=float, default=None, help="Arithmetic grid start (overrides --alpha if set with --alpha-end/--alpha-step).")
    ap.add_argument("--alpha-end", type=float, default=None, help="Arithmetic grid end (inclusive).")
    ap.add_argument("--alpha-step", type=float, default=None, help="Arithmetic grid step (>0).")
    ap.add_argument(
        "--per-seed",
        action="store_true",
        help="When sweeping --alpha-grid, also print per-seed lines (can be verbose).",
    )
    ap.add_argument("--no-progress", action="store_true", help="Disable progress bar output (stderr).")
    ap.add_argument("--pos-top-frac", type=float, default=0.35, help="Positive label fraction among promotions (training).")
    ap.add_argument(
        "--label-kind",
        choices=["uplift", "next_score"],
        default="uplift",
        help="Training label for reranker: uplift (o2-o1) or next_score (o2). next_score is often less noisy.",
    )
    args = ap.parse_args()

    # --- 0) load dataset (OpenML) ---
    openml_id = int(args.openml_id)
    openml_name = str(args.openml_name).strip()
    if openml_id <= 0 and not openml_name:
        # default dataset (small and common)
        openml_id = 31  # credit-g v1

    print("loading dataset from OpenML...", flush=True)
    t_load0 = perf_counter()
    try:
        bunch = fetch_openml(data_id=int(openml_id), as_frame=True, data_home=str(args.data_home)) if openml_id > 0 else fetch_openml(
            name=openml_name, as_frame=True, data_home=str(args.data_home)
        )
    except Exception as e:
        raise SystemExit(
            "OpenML 下载失败（可能是网络/证书/代理问题）。你可以改用另一个 --openml-id。\n"
            f"原始错误：{e}"
        )
    t_load = perf_counter() - t_load0

    X_df: pd.DataFrame = bunch.data  # type: ignore[assignment]
    y_ser = bunch.target  # type: ignore[attr-defined]
    if isinstance(y_ser, pd.DataFrame):
        if y_ser.shape[1] != 1:
            raise SystemExit(f"OpenML target has {y_ser.shape[1]} columns; please choose a single target.")
        y_ser = y_ser.iloc[:, 0]
    y = y_ser.to_numpy()

    print(
        f"loaded: openml_id={getattr(bunch, 'data_id', openml_id)} X={X_df.shape} y={np.asarray(y).shape} in {t_load:.2f}s",
        flush=True,
    )

    # --- 1) preprocess to numeric arrays once ---
    X_train_df, X_tmp_df, y_train, y_tmp = train_test_split(
        X_df, y, test_size=0.40, random_state=int(args.seed0), stratify=y
    )
    X_val_df, X_test_df, y_val, y_test = train_test_split(
        X_tmp_df, y_tmp, test_size=0.50, random_state=int(args.seed0), stratify=y_tmp
    )

    pre = _build_preprocessor(X_train_df)
    t_pre0 = perf_counter()
    X_train = pre.fit_transform(X_train_df)
    X_val = pre.transform(X_val_df)
    X_test = pre.transform(X_test_df)
    t_pre = perf_counter() - t_pre0
    print(f"preprocessed to numeric matrices in {t_pre:.2f}s", flush=True)

    # Some transformers return sparse
    try:
        X_train = X_train.toarray()
        X_val = X_val.toarray()
        X_test = X_test.toarray()
    except Exception:
        pass

    # --- 2) define search space (GBDT-like) ---
    search_space: Dict[str, Tuple[float, float, str]] = {
        "learning_rate": (0.01, 0.3, "log"),
        "subsample": (0.5, 1.0, "uniform"),
        "max_depth": (1.0, 6.0, "uniform"),
        "min_samples_leaf": (1.0, 60.0, "log"),
    }

    train_seeds = [int(args.seed0) + i for i in range(int(args.train_seeds))]
    test_seeds = [int(args.seed0) + int(args.train_seeds) + i for i in range(int(args.test_seeds))]

    # --- 3) train reranker on baseline ASHA promotions (same dataset, different HPO RNG seeds) ---
    X_rows_all: List[dict] = []
    y_rows_all: List[int] = []
    t_train0 = perf_counter()
    prog = _Progress(enabled=(not bool(args.no_progress)))
    prog.begin()
    for i, s in enumerate(train_seeds, start=1):
        prog.update(f"[train reranker] seed {i}/{len(train_seeds)}  {_fmt_eta(i-1, len(train_seeds), prog.start)}")
        objective = _make_gbdt_objective(X_train=X_train, y_train=y_train, X_val=X_val, y_val=y_val, seed=int(s))
        X_rows, y_rows = collect_promotion_training_data(
            objective=objective,
            search_space=search_space,
            X=X_train,
            y=y_train,
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
    t_train = perf_counter() - t_train0

    # --- 4) evaluate guided vs baseline on held-out HPO RNG seeds ---
    # baseline per-seed can be reused across multiple alphas
    has_grid_str = bool(str(args.alpha_grid).strip())
    has_arith = (args.alpha_start is not None) or (args.alpha_end is not None) or (args.alpha_step is not None)
    if has_grid_str and has_arith:
        raise SystemExit("请只选择一种网格方式：要么用 --alpha-grid，要么用 --alpha-start/--alpha-end/--alpha-step。")

    alphas: List[float] = []
    if has_grid_str:
        alphas = _parse_float_grid(str(args.alpha_grid))
    elif has_arith:
        if args.alpha_start is None or args.alpha_end is None or args.alpha_step is None:
            raise SystemExit("使用等差网格时必须同时提供 --alpha-start --alpha-end --alpha-step。")
        alphas = _alpha_arith_grid(start=float(args.alpha_start), end=float(args.alpha_end), step=float(args.alpha_step))
    else:
        alphas = [float(args.alpha)]

    # basic sanity
    if not alphas:
        raise SystemExit("alpha 网格为空，请检查 --alpha-grid 或等差参数。")
    bad = [a for a in alphas if (a < 0.0 or a > 1.0)]
    if bad:
        raise SystemExit(f"alpha 必须在 [0,1] 内，发现非法值：{bad}")

    t_eval0 = perf_counter()

    base_by_seed: Dict[int, Dict[str, float]] = {}
    prog = _Progress(enabled=(not bool(args.no_progress)))
    prog.begin()
    for i, s in enumerate(test_seeds, start=1):
        prog.update(f"[baseline] seed {i}/{len(test_seeds)}  {_fmt_eta(i-1, len(test_seeds), prog.start)}")
        objective = _make_gbdt_objective(X_train=X_train, y_train=y_train, X_val=X_val, y_val=y_val, seed=int(s))
        base = run_asha(
            objective=objective,
            search_space=search_space,
            seed=int(s),
            n0=int(args.n0),
            eta=int(args.eta),
            min_resource=int(args.min_resource),
            max_resource=int(args.max_resource),
        )
        base_final_obs = base.best_at_resource(int(args.max_resource))
        base_final = float(base_final_obs.score) if base_final_obs is not None else float("nan")
        base_by_seed[int(s)] = {"base_best": float(base.best_score), "base_final": float(base_final)}
    prog.done()

    # guided results per alpha
    results_by_alpha: Dict[float, List[OneSeedResult]] = {float(a): [] for a in alphas}

    total_jobs = len(alphas) * len(test_seeds)
    done_jobs = 0
    prog = _Progress(enabled=(not bool(args.no_progress)))
    prog.begin()
    for ai, a in enumerate(alphas, start=1):
        for si, s in enumerate(test_seeds, start=1):
            done_jobs += 1
            prog.update(
                f"[guided] alpha {ai}/{len(alphas)} seed {si}/{len(test_seeds)} "
                f"({done_jobs}/{total_jobs})  {_fmt_eta(done_jobs, total_jobs, prog.start)}"
            )
            objective = _make_gbdt_objective(X_train=X_train, y_train=y_train, X_val=X_val, y_val=y_val, seed=int(s))
            guided = run_guided_asha(
                objective=objective,
                search_space=search_space,
                X=X_train,
                y=y_train,
                seed=int(s),
                reranker=reranker,
                n0=int(args.n0),
                eta=int(args.eta),
                min_resource=int(args.min_resource),
                max_resource=int(args.max_resource),
                mode=str(args.mode),
                shortlist_k=int(args.shortlist_k),
                alpha=float(a),
                max_swaps=(None if int(args.max_swaps) < 0 else int(args.max_swaps)),
                min_mix_margin=float(args.min_mix_margin),
                min_proba=float(args.min_proba),
            )
            guided_final_obs = guided.best_at_resource(int(args.max_resource))
            guided_final = float(guided_final_obs.score) if guided_final_obs is not None else float("nan")

            b = base_by_seed[int(s)]
            r = OneSeedResult(
                seed=int(s),
                base_best=float(b["base_best"]),
                base_final=float(b["base_final"]),
                guided_best=float(guided.best_score),
                guided_final=float(guided_final),
                guided_disagree_rate=float(guided.promote_disagree_rate),
            )
            results_by_alpha[float(a)].append(r)

            # keep old per-seed printing when not sweeping; in sweep mode, only print if --per-seed
            if (len(alphas) == 1) or bool(args.per_seed):
                prefix = f"hpo_seed={s}"
                if len(alphas) > 1:
                    prefix += f" alpha={float(a):.4g}"
                print(
                    f"{prefix}  "
                    f"val_best(base/guided)={r.base_best:.4f}/{r.guided_best:.4f}  "
                    f"val_final@R(base/guided)={r.base_final:.4f}/{r.guided_final:.4f}  "
                    f"promote_disagree={r.guided_disagree_rate:.2f}"
                )
    prog.done()

    t_eval = perf_counter() - t_eval0

    # baseline summary (independent of alpha)
    base_best_m, base_best_s = _summary([base_by_seed[s]["base_best"] for s in base_by_seed])
    base_fin_m, base_fin_s = _summary([base_by_seed[s]["base_final"] for s in base_by_seed])

    print()
    print("=== Guided-ASHA (OpenML) Summary ===")
    ds = f"openml_id={int(getattr(bunch, 'data_id', openml_id))}" if openml_id > 0 else f"openml_name={openml_name}"
    print(f"dataset: {ds}")
    print(f"train_seeds={len(train_seeds)} test_seeds={len(test_seeds)} n0={args.n0} eta={args.eta} R={args.max_resource}")
    alpha_disp = "[" + ", ".join(f"{float(a):.4g}" for a in alphas) + "]"
    max_swaps_disp = "k" if int(args.max_swaps) < 0 else str(int(args.max_swaps))
    print(
        f"guided: mode={args.mode} shortlist_k={args.shortlist_k} alpha_grid={alpha_disp} "
        f"label_kind={args.label_kind} max_swaps={max_swaps_disp} min_mix_margin={args.min_mix_margin} min_proba={args.min_proba}"
    )
    print()
    print(f"train_reranker_time_sec: {t_train:.3f}")
    print(f"eval_time_sec          : {t_eval:.3f}")
    print()
    print(f"val_best(base)      : mean={base_best_m:.4f} std={base_best_s:.4f}")
    print(f"val_final@R(base)   : mean={base_fin_m:.4f} std={base_fin_s:.4f}")

    # alpha sweep table
    rows_out: List[Dict[str, float]] = []
    for a in alphas:
        rs = results_by_alpha[float(a)]
        guided_best_m, guided_best_s = _summary([r.guided_best for r in rs])
        guided_fin_m, guided_fin_s = _summary([r.guided_final for r in rs])
        dis_m, dis_s = _summary([r.guided_disagree_rate for r in rs])
        delta_fin = guided_fin_m - base_fin_m
        rows_out.append(
            {
                "alpha": float(a),
                "guided_best_mean": float(guided_best_m),
                "guided_best_std": float(guided_best_s),
                "guided_final_mean": float(guided_fin_m),
                "guided_final_std": float(guided_fin_s),
                "delta_final_mean": float(delta_fin),
                "disagree_mean": float(dis_m),
                "disagree_std": float(dis_s),
            }
        )

    # pick best alpha: maximize delta_final_mean, then guided_final_mean, then minimize disagree_mean
    rows_out.sort(key=lambda r: (r["delta_final_mean"], r["guided_final_mean"], -r["disagree_mean"]), reverse=True)
    best = rows_out[0]

    print()
    print("alpha sweep (sorted by delta_final_mean):")
    for r in rows_out:
        print(
            f"- alpha={r['alpha']:.4g}  "
            f"final@R={r['guided_final_mean']:.4f}+/-{r['guided_final_std']:.4f}  "
            f"delta_final={r['delta_final_mean']:+.4f}  "
            f"best={r['guided_best_mean']:.4f}+/-{r['guided_best_std']:.4f}  "
            f"disagree={r['disagree_mean']:.3f}+/-{r['disagree_std']:.3f}"
        )

    print()
    print(f"best_alpha: {best['alpha']:.4g} (delta_final_mean={best['delta_final_mean']:+.4f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

