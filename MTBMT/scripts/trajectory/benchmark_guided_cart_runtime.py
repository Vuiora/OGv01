from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
from sklearn.datasets import make_classification
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier

from mtbmt.guided_cart import GuidedCARTClassifier, SplitReranker, collect_cart_split_training_data


@dataclass
class Timing:
    name: str
    fit_mean: float
    fit_std: float
    pred_mean: float
    pred_std: float
    extra: Dict[str, float]


def _timeit(fn, repeats: int) -> Tuple[float, float]:
    ts: List[float] = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        fn()
        ts.append(time.perf_counter() - t0)
    return float(np.mean(ts)), float(np.std(ts))


def main() -> int:
    ap = argparse.ArgumentParser(description="Runtime benchmark: sklearn CART vs Guided-CART (rerank/replace).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n-samples", type=int, default=20000)
    ap.add_argument("--n-features", type=int, default=20)
    ap.add_argument("--n-informative", type=int, default=10)
    ap.add_argument("--n-redundant", type=int, default=2)
    ap.add_argument("--n-classes", type=int, default=2)
    ap.add_argument("--depth", type=int, default=8)
    ap.add_argument("--shortlist-k", type=int, default=8)
    ap.add_argument("--thresholds", type=int, default=16, help="max thresholds per feature (Guided-CART candidate generation).")
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--warmup", type=int, default=1)
    args = ap.parse_args()

    X, y = make_classification(
        n_samples=args.n_samples,
        n_features=args.n_features,
        n_informative=args.n_informative,
        n_redundant=args.n_redundant,
        n_classes=args.n_classes,
        random_state=args.seed,
    )
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=args.seed, stratify=y)

    print("=== Runtime Benchmark: CART vs Guided-CART ===")
    print(
        "data:",
        f"n={args.n_samples}",
        f"p={args.n_features}",
        f"classes={args.n_classes}",
        "| depth=",
        args.depth,
        "shortlist_k=",
        args.shortlist_k,
        "thresholds=",
        args.thresholds,
    )
    print("repeats:", args.repeats, "warmup:", args.warmup)
    print()

    # -----------------------
    # Baseline: sklearn CART
    # -----------------------
    def _sk_fit():
        clf = DecisionTreeClassifier(max_depth=args.depth, criterion="gini", random_state=args.seed)
        clf.fit(X_train, y_train)
        return clf

    def _sk_pred(clf):
        _ = clf.predict(X_test)

    # warmup
    for _ in range(args.warmup):
        c0 = _sk_fit()
        _sk_pred(c0)

    clf_holder = {"clf": None}

    def _sk_fit_wrap():
        clf_holder["clf"] = _sk_fit()

    def _sk_pred_wrap():
        _sk_pred(clf_holder["clf"])

    sk_fit_mean, sk_fit_std = _timeit(_sk_fit_wrap, args.repeats)
    sk_pred_mean, sk_pred_std = _timeit(_sk_pred_wrap, args.repeats)
    sk_acc = float((clf_holder["clf"].predict(X_test) == y_test).mean())

    results: List[Timing] = [
        Timing(
            name="sklearn_CART_gini",
            fit_mean=sk_fit_mean,
            fit_std=sk_fit_std,
            pred_mean=sk_pred_mean,
            pred_std=sk_pred_std,
            extra={"acc": sk_acc},
        )
    ]

    # -----------------------------------------
    # Guided-CART: stage timings + end-to-end
    # -----------------------------------------
    # Stage A: collect node candidate training data (from baseline CART decisions)
    def _collect():
        _ = collect_cart_split_training_data(
            X_train,
            y_train,
            max_depth=args.depth,
            min_samples_split=2,
            min_samples_leaf=1,
            max_thresholds_per_feature=args.thresholds,
        )

    for _ in range(args.warmup):
        _collect()
    collect_mean, collect_std = _timeit(_collect, args.repeats)

    # Stage B: fit reranker (RF) once
    rows, yy = collect_cart_split_training_data(
        X_train,
        y_train,
        max_depth=args.depth,
        min_samples_split=2,
        min_samples_leaf=1,
        max_thresholds_per_feature=args.thresholds,
    )

    def _fit_reranker():
        rr = SplitReranker(random_state=args.seed, n_estimators=300)
        rr.fit(rows, yy)
        return rr

    for _ in range(args.warmup):
        rr0 = _fit_reranker()
        _ = rr0.score_candidates(rows[:10])

    rr_holder = {"rr": None}

    def _fit_reranker_wrap():
        rr_holder["rr"] = _fit_reranker()

    fit_rr_mean, fit_rr_std = _timeit(_fit_reranker_wrap, args.repeats)
    rr = rr_holder["rr"]

    # Stage C/D: build guided tree + predict (with fixed reranker)
    def _guided_fit(mode: str, alpha: float):
        g = GuidedCARTClassifier(
            max_depth=args.depth,
            mode=mode,
            shortlist_k=args.shortlist_k,
            alpha=alpha,
            max_thresholds_per_feature=args.thresholds,
            reranker=rr,
        )
        g.fit(X_train, y_train)
        return g

    def _guided_pred(g):
        _ = g.predict(X_test)

    for mode, alpha in [("rerank", 1.0), ("rerank", 0.0), ("replace", 1.0)]:
        # warmup
        for _ in range(args.warmup):
            g0 = _guided_fit(mode, alpha)
            _guided_pred(g0)

        g_holder = {"g": None}

        def _fit_wrap():
            g_holder["g"] = _guided_fit(mode, alpha)

        def _pred_wrap():
            _guided_pred(g_holder["g"])

        fit_mean, fit_std = _timeit(_fit_wrap, args.repeats)
        pred_mean, pred_std = _timeit(_pred_wrap, args.repeats)
        acc = float((g_holder["g"].predict(X_test) == y_test).mean())
        stats = g_holder["g"].tree_stats()
        results.append(
            Timing(
                name=f"guided_CART[{mode}]-alpha={alpha}",
                fit_mean=fit_mean,
                fit_std=fit_std,
                pred_mean=pred_mean,
                pred_std=pred_std,
                extra={"acc": acc, "n_nodes": float(stats["n_nodes"]), "max_depth": float(stats["max_depth"])},
            )
        )

    print("Stage timings (Guided-CART only):")
    print(f"- collect_candidates: mean={collect_mean:.6f}s std={collect_std:.6f}s")
    print(f"- fit_reranker(RF):  mean={fit_rr_mean:.6f}s std={fit_rr_std:.6f}s   (this is a one-time cost if reused)")
    print()

    print("Model timings (fit/predict):")
    for r in results:
        extra = " ".join([f"{k}={v:.4f}" for k, v in r.extra.items()])
        print(
            f"- {r.name:24s}  fit={r.fit_mean:.6f}±{r.fit_std:.6f}s"
            f"  pred={r.pred_mean:.6f}±{r.pred_std:.6f}s  {extra}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


