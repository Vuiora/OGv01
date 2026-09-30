from __future__ import annotations

import argparse
from dataclasses import dataclass
from time import perf_counter
from typing import Dict, List, Sequence, Tuple

import numpy as np
from sklearn.datasets import make_classification
from sklearn.linear_model import SGDClassifier
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler

from mtbmt.hpo.guided_asha import (
    HpoConfig,
    HpoReranker,
    collect_promotion_training_data,
    run_asha,
    run_guided_asha,
)


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


def _make_objective_for_dataset(
    *,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    seed: int,
) -> callable:
    """
    A tiny iterative objective suitable for ASHA:
    - model: SGDClassifier (logistic regression via SGD)
    - resource: number of epochs (full passes over training set)
    - score: validation accuracy after the last epoch
    - trajectory: per-epoch validation accuracies
    """

    X_train = np.asarray(X_train, dtype=float)
    y_train = np.asarray(y_train, dtype=int)
    X_val = np.asarray(X_val, dtype=float)
    y_val = np.asarray(y_val, dtype=int)

    classes = np.unique(y_train)
    rng = np.random.default_rng(int(seed))

    def objective(cfg: HpoConfig, resource: int, _seed: int) -> Tuple[float, Sequence[float]]:
        params = cfg.params
        alpha = float(params.get("alpha", 1e-4))
        l1_ratio = float(params.get("l1_ratio", 0.15))
        eta0 = float(params.get("eta0", 0.01))

        clf = SGDClassifier(
            loss="log_loss",
            penalty="elasticnet",
            alpha=alpha,
            l1_ratio=l1_ratio,
            learning_rate="constant",
            eta0=eta0,
            random_state=int(seed),
            fit_intercept=True,
            average=False,
        )

        # train for `resource` epochs with deterministic shuffling
        traj: List[float] = []
        idx = np.arange(len(X_train))
        for _ in range(max(1, int(resource))):
            rng.shuffle(idx)
            Xt = X_train[idx]
            yt = y_train[idx]
            clf.partial_fit(Xt, yt, classes=classes)
            traj.append(float(clf.score(X_val, y_val)))
        return float(traj[-1]), traj

    return objective


def main() -> int:
    ap = argparse.ArgumentParser(description="Evaluate baseline ASHA vs Guided-ASHA on a small synthetic task.")
    ap.add_argument("--train-seeds", type=int, default=6, help="How many dataset seeds used to train the reranker.")
    ap.add_argument("--test-seeds", type=int, default=6, help="How many dataset seeds used to evaluate guided vs baseline.")
    ap.add_argument("--seed0", type=int, default=42, help="Base seed.")
    ap.add_argument("--n-samples", type=int, default=6000)
    ap.add_argument("--n-features", type=int, default=30)
    ap.add_argument("--class-sep", type=float, default=1.0)
    ap.add_argument("--flip-y", type=float, default=0.05)

    ap.add_argument("--n0", type=int, default=27, help="Initial number of configs.")
    ap.add_argument("--eta", type=int, default=3)
    ap.add_argument("--min-resource", type=int, default=1)
    ap.add_argument("--max-resource", type=int, default=27)

    ap.add_argument("--mode", choices=["rerank", "replace"], default="rerank")
    ap.add_argument("--shortlist-k", type=int, default=9)
    ap.add_argument("--alpha", type=float, default=0.60, help="mix weight: alpha*proba + (1-alpha)*score_norm")
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
    ap.add_argument("--pos-top-frac", type=float, default=0.35, help="Positive label fraction among promotions (training).")
    ap.add_argument(
        "--label-kind",
        choices=["uplift", "next_score"],
        default="uplift",
        help="Training label for reranker: uplift (o2-o1) or next_score (o2). next_score is often less noisy.",
    )
    args = ap.parse_args()

    search_space: Dict[str, Tuple[float, float, str]] = {
        "alpha": (1e-6, 1e-2, "log"),
        "eta0": (1e-4, 1e-1, "log"),
        "l1_ratio": (0.0, 1.0, "uniform"),
    }

    train_seeds = [int(args.seed0) + i for i in range(int(args.train_seeds))]
    test_seeds = [int(args.seed0) + int(args.train_seeds) + i for i in range(int(args.test_seeds))]

    # --- 1) train reranker on baseline ASHA promotions ---
    X_rows_all: List[dict] = []
    y_rows_all: List[int] = []
    t_train0 = perf_counter()
    for seed in train_seeds:
        X, y = make_classification(
            n_samples=int(args.n_samples),
            n_features=int(args.n_features),
            n_informative=max(2, int(0.4 * int(args.n_features))),
            n_redundant=max(1, int(0.1 * int(args.n_features))),
            n_classes=2,
            class_sep=float(args.class_sep),
            flip_y=float(args.flip_y),
            random_state=int(seed),
        )
        X_train, X_tmp, y_train, y_tmp = train_test_split(X, y, test_size=0.40, random_state=seed, stratify=y)
        X_val, X_test, y_val, y_test = train_test_split(X_tmp, y_tmp, test_size=0.50, random_state=seed, stratify=y_tmp)
        scaler = StandardScaler().fit(X_train)
        X_train_s = scaler.transform(X_train)
        X_val_s = scaler.transform(X_val)

        objective = _make_objective_for_dataset(X_train=X_train_s, y_train=y_train, X_val=X_val_s, y_val=y_val, seed=seed)
        X_rows, y_rows = collect_promotion_training_data(
            objective=objective,
            search_space=search_space,
            X=X_train_s,
            y=y_train,
            seed=seed,
            n0=int(args.n0),
            eta=int(args.eta),
            min_resource=int(args.min_resource),
            max_resource=int(args.max_resource),
            positive_top_frac=float(args.pos_top_frac),
            label_kind=str(args.label_kind),
        )
        X_rows_all.extend(X_rows)
        y_rows_all.extend(y_rows)

    reranker = HpoReranker(random_state=int(args.seed0))
    reranker.fit(X_rows_all, y_rows_all)
    t_train = perf_counter() - t_train0

    # --- 2) evaluate on held-out dataset seeds ---
    results: List[OneSeedResult] = []
    t_eval0 = perf_counter()
    for seed in test_seeds:
        X, y = make_classification(
            n_samples=int(args.n_samples),
            n_features=int(args.n_features),
            n_informative=max(2, int(0.4 * int(args.n_features))),
            n_redundant=max(1, int(0.1 * int(args.n_features))),
            n_classes=2,
            class_sep=float(args.class_sep),
            flip_y=float(args.flip_y),
            random_state=int(seed),
        )
        X_train, X_tmp, y_train, y_tmp = train_test_split(X, y, test_size=0.40, random_state=seed, stratify=y)
        X_val, X_test, y_val, y_test = train_test_split(X_tmp, y_tmp, test_size=0.50, random_state=seed, stratify=y_tmp)
        scaler = StandardScaler().fit(X_train)
        X_train_s = scaler.transform(X_train)
        X_val_s = scaler.transform(X_val)
        X_test_s = scaler.transform(X_test)

        objective = _make_objective_for_dataset(X_train=X_train_s, y_train=y_train, X_val=X_val_s, y_val=y_val, seed=seed)

        base = run_asha(
            objective=objective,
            search_space=search_space,
            seed=seed,
            n0=int(args.n0),
            eta=int(args.eta),
            min_resource=int(args.min_resource),
            max_resource=int(args.max_resource),
        )
        guided = run_guided_asha(
            objective=objective,
            search_space=search_space,
            X=X_train_s,
            y=y_train,
            seed=seed,
            reranker=reranker,
            n0=int(args.n0),
            eta=int(args.eta),
            min_resource=int(args.min_resource),
            max_resource=int(args.max_resource),
            mode=str(args.mode),
            shortlist_k=int(args.shortlist_k),
            alpha=float(args.alpha),
            max_swaps=(None if int(args.max_swaps) < 0 else int(args.max_swaps)),
            min_mix_margin=float(args.min_mix_margin),
            min_proba=float(args.min_proba),
        )

        # For a fair "final" metric, compare the best score at max_resource.
        base_final_obs = base.best_at_resource(int(args.max_resource))
        guided_final_obs = guided.best_at_resource(int(args.max_resource))
        base_final = float(base_final_obs.score) if base_final_obs is not None else float("nan")
        guided_final = float(guided_final_obs.score) if guided_final_obs is not None else float("nan")

        # Optional sanity: evaluate the best-found configs on test set.
        # (We only use it for printed context; HPO is driven by val.)
        def _test_score(cfg: HpoConfig) -> float:
            # train at full resource on train+val, report test acc
            X_tv = np.vstack([X_train_s, X_val_s])
            y_tv = np.concatenate([y_train, y_val])
            obj_tv = _make_objective_for_dataset(X_train=X_tv, y_train=y_tv, X_val=X_test_s, y_val=y_test, seed=seed)
            sc, _traj = obj_tv(cfg, int(args.max_resource), seed)
            return float(sc)

        base_test = _test_score(base.best_config)
        guided_test = _test_score(guided.best_config)

        results.append(
            OneSeedResult(
                seed=seed,
                base_best=float(base.best_score),
                base_final=base_final,
                guided_best=float(guided.best_score),
                guided_final=guided_final,
                guided_disagree_rate=float(guided.promote_disagree_rate),
            )
        )
        print(
            f"seed={seed}  "
            f"val_best(base/guided)={base.best_score:.4f}/{guided.best_score:.4f}  "
            f"val_final@R(base/guided)={base_final:.4f}/{guided_final:.4f}  "
            f"test@R(base/guided)={base_test:.4f}/{guided_test:.4f}  "
            f"promote_disagree={guided.promote_disagree_rate:.2f}"
        )

    t_eval = perf_counter() - t_eval0

    base_best_m, base_best_s = _summary([r.base_best for r in results])
    guided_best_m, guided_best_s = _summary([r.guided_best for r in results])
    base_fin_m, base_fin_s = _summary([r.base_final for r in results])
    guided_fin_m, guided_fin_s = _summary([r.guided_final for r in results])
    dis_m, dis_s = _summary([r.guided_disagree_rate for r in results])

    print()
    print("=== Guided-ASHA Evaluation Summary ===")
    print(f"train_seeds={len(train_seeds)} test_seeds={len(test_seeds)} n0={args.n0} eta={args.eta} R={args.max_resource}")
    max_swaps_disp = "k" if int(args.max_swaps) < 0 else str(int(args.max_swaps))
    print(
        f"guided: mode={args.mode} shortlist_k={args.shortlist_k} alpha={args.alpha} label_kind={args.label_kind} "
        f"max_swaps={max_swaps_disp} min_mix_margin={args.min_mix_margin} min_proba={args.min_proba}"
    )
    print()
    print(f"train_reranker_time_sec: {t_train:.3f}")
    print(f"eval_time_sec          : {t_eval:.3f}")
    print()
    print(f"val_best(base)   : mean={base_best_m:.4f} std={base_best_s:.4f}")
    print(f"val_best(guided) : mean={guided_best_m:.4f} std={guided_best_s:.4f}")
    print(f"val_final@R(base)   : mean={base_fin_m:.4f} std={base_fin_s:.4f}")
    print(f"val_final@R(guided) : mean={guided_fin_m:.4f} std={guided_fin_s:.4f}")
    print(f"promote_disagree_rate : mean={dis_m:.3f} std={dis_s:.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

