from __future__ import annotations

import argparse
from collections import Counter
import sys
from time import perf_counter
from typing import List, Tuple

import numpy as np
from sklearn.datasets import make_classification
from sklearn.model_selection import train_test_split

from mtbmt.guided_cart import GuidedCARTClassifier


def _diff_count(a: List[Tuple[int, int, float]], b: List[Tuple[int, int, float]]) -> int:
    n = min(len(a), len(b))
    d = 0
    for i in range(n):
        if a[i] != b[i]:
            d += 1
    d += abs(len(a) - len(b))
    return d


def _step(msg: str, *, enabled: bool, start: float) -> None:
    if not enabled:
        return
    elapsed = perf_counter() - start
    sys.stderr.write(f"[progress] {msg} (elapsed={elapsed:.1f}s)\n")
    sys.stderr.flush()


def main() -> int:
    ap = argparse.ArgumentParser(description="Analyze why Guided-CART alpha changes do/do-not change splits.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--shortlist-k", type=int, default=6)
    ap.add_argument("--thresholds", type=int, default=16)
    ap.add_argument(
        "--threshold-strategy",
        choices=["quantile", "unique_midpoints_cap", "unique_midpoints"],
        default="quantile",
    )
    ap.add_argument("--mode", choices=["rerank", "replace"], default="rerank")
    ap.add_argument("--reranker-target", choices=["gini", "val_gain"], default="gini")
    ap.add_argument("--val-fraction", type=float, default=0.25)
    ap.add_argument("--no-progress", action="store_true", help="Disable progress output.")
    args = ap.parse_args()
    t0 = perf_counter()

    X, y = make_classification(
        n_samples=20000,
        n_features=20,
        n_informative=10,
        n_redundant=2,
        n_classes=2,
        random_state=args.seed,
    )
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=args.seed, stratify=y)

    # Train a shared reranker once (inside first model), then reuse it for fair comparison.
    _step("fit base (train shared reranker) 1/3", enabled=(not bool(args.no_progress)), start=t0)
    base = GuidedCARTClassifier(
        max_depth=args.depth,
        mode=args.mode,
        shortlist_k=args.shortlist_k,
        alpha=1.0,
        max_thresholds_per_feature=args.thresholds,
        threshold_strategy=args.threshold_strategy,
        reranker_target=args.reranker_target,
        val_fraction=float(args.val_fraction),
    )
    base.fit(X_train, y_train)
    rr = base.reranker

    _step("fit alpha=0 model 2/3", enabled=(not bool(args.no_progress)), start=t0)
    m0 = GuidedCARTClassifier(
        max_depth=args.depth,
        mode=args.mode,
        shortlist_k=args.shortlist_k,
        alpha=0.0,
        max_thresholds_per_feature=args.thresholds,
        threshold_strategy=args.threshold_strategy,
        reranker=rr,
        reranker_target=args.reranker_target,
        val_fraction=float(args.val_fraction),
    ).fit(X_train, y_train)

    _step("fit alpha=1 model 3/3", enabled=(not bool(args.no_progress)), start=t0)
    m1 = GuidedCARTClassifier(
        max_depth=args.depth,
        mode=args.mode,
        shortlist_k=args.shortlist_k,
        alpha=1.0,
        max_thresholds_per_feature=args.thresholds,
        threshold_strategy=args.threshold_strategy,
        reranker=rr,
        reranker_target=args.reranker_target,
        val_fraction=float(args.val_fraction),
    ).fit(X_train, y_train)

    s0 = m0.split_signatures_preorder()
    s1 = m1.split_signatures_preorder()

    acc0 = float((m0.predict(X_test) == y_test).mean())
    acc1 = float((m1.predict(X_test) == y_test).mean())

    print("=== Guided-CART alpha analysis ===")
    print("mode:", args.mode, "depth:", args.depth, "shortlist_k:", args.shortlist_k, "reranker_target:", args.reranker_target)
    print("threshold_strategy:", args.threshold_strategy, "thresholds:", args.thresholds)
    print("alpha=0 acc:", acc0, "splits:", len(s0))
    print("alpha=1 acc:", acc1, "splits:", len(s1))
    print("split_diff_count:", _diff_count(s0, s1))

    if len(s0) and len(s1):
        # show most common split (depth, feat) pairs to see if models collapse to same features
        c0 = Counter([(d, f) for (d, f, _t) in s0])
        c1 = Counter([(d, f) for (d, f, _t) in s1])
        top0 = c0.most_common(5)
        top1 = c1.most_common(5)
        print("top_splits_alpha0(depth,feat):", top0)
        print("top_splits_alpha1(depth,feat):", top1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


