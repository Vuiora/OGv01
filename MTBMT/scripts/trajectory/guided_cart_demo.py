from __future__ import annotations

import argparse

from sklearn.datasets import make_classification
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier

from mtbmt.guided_cart import GuidedCARTClassifier


def main() -> int:
    ap = argparse.ArgumentParser(description="Guided-CART demo: rerank/replace split decisions using a learned tendency model.")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--mode", choices=["rerank", "replace"], default="rerank")
    ap.add_argument("--alpha", type=float, default=1.0, help="mix weight: alpha*proba + (1-alpha)*gain_norm")
    ap.add_argument("--depth", type=int, default=8)
    ap.add_argument("--shortlist-k", type=int, default=8)
    ap.add_argument("--thresholds", type=int, default=16)
    ap.add_argument(
        "--threshold-strategy",
        choices=["quantile", "unique_midpoints_cap", "unique_midpoints"],
        default="quantile",
    )
    ap.add_argument("--reranker-target", choices=["gini", "val_gain"], default="gini")
    ap.add_argument("--val-fraction", type=float, default=0.25)
    args = ap.parse_args()

    X, y = make_classification(
        n_samples=20000,
        n_features=20,
        n_informative=10,
        n_redundant=2,
        n_classes=2,
        random_state=args.seed,
    )
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.25, random_state=args.seed, stratify=y)

    # Baseline CART (gini)
    sk = DecisionTreeClassifier(max_depth=args.depth, criterion="gini", random_state=args.seed)
    sk.fit(X_train, y_train)
    acc_sk = float(sk.score(X_test, y_test))

    guided = GuidedCARTClassifier(
        max_depth=args.depth,
        mode=args.mode,
        shortlist_k=args.shortlist_k,
        alpha=float(args.alpha),
        max_thresholds_per_feature=int(args.thresholds),
        threshold_strategy=str(args.threshold_strategy),
        reranker_target=str(args.reranker_target),
        val_fraction=float(args.val_fraction),
    )
    guided.fit(X_train, y_train)
    pred = guided.predict(X_test)
    acc_g = float((pred == y_test).mean())

    print("=== Guided-CART Demo ===")
    print(
        "mode:",
        args.mode,
        "alpha:",
        args.alpha,
        "depth:",
        args.depth,
        "shortlist_k:",
        args.shortlist_k,
        "threshold_strategy:",
        args.threshold_strategy,
        "thresholds:",
        args.thresholds,
        "reranker_target:",
        args.reranker_target,
    )
    print("sklearn_cart_gini_acc:", acc_sk)
    print("guided_cart_acc:", acc_g)
    print("guided_tree_stats:", guided.tree_stats())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


