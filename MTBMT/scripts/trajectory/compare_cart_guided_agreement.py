from __future__ import annotations

import argparse
from dataclasses import dataclass
import sys
from time import perf_counter
from typing import Dict, List, Tuple

import numpy as np
from sklearn.datasets import make_classification
from sklearn.model_selection import train_test_split
from sklearn.tree import DecisionTreeClassifier

from mtbmt.guided_cart import GuidedCARTClassifier

try:
    # Optional dependency. If installed, we use a nicer progress bar.
    from tqdm import tqdm  # type: ignore
except Exception:  # pragma: no cover
    tqdm = None


@dataclass
class OneRun:
    seed: int
    acc_cart: float
    acc_guided: float
    agreement: float
    disagree_rate: float


def _summary(xs: List[float]) -> Tuple[float, float, float]:
    a = np.asarray(xs, dtype=float)
    return float(np.mean(a)), float(np.std(a)), float(np.min(a))


def _iter_progress(items: List[int], *, desc: str, enabled: bool):
    if not enabled:
        for x in items:
            yield x
        return

    if tqdm is not None:
        # Windows PowerShell can display Unicode blocks as garbled chars; use ascii mode.
        for x in tqdm(
            items,
            total=len(items),
            desc=desc,
            ascii=True,
            leave=False,
            file=sys.stderr,
        ):
            yield int(x)
        return

    # Fallback: pure stdlib progress line with ETA.
    total = max(1, len(items))
    start = perf_counter()
    for i, x in enumerate(items, start=1):
        now = perf_counter()
        elapsed = now - start
        rate = elapsed / max(1, i)
        eta = rate * max(0, total - i)
        pct = 100.0 * i / total
        sys.stderr.write(
            f"\r{desc} {i}/{total} ({pct:5.1f}%)  elapsed={elapsed:6.1f}s  eta={eta:6.1f}s"
        )
        sys.stderr.flush()
        yield int(x)
    sys.stderr.write("\n")
    sys.stderr.flush()


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Compare sample-level prediction agreement: sklearn CART(gini) vs Guided-CART."
    )
    ap.add_argument("--seeds", type=int, default=10, help="How many random seeds (datasets) to test.")
    ap.add_argument("--seed0", type=int, default=42, help="Base seed.")

    ap.add_argument("--n-samples", type=int, default=20000)
    ap.add_argument("--n-features", type=int, default=20)
    ap.add_argument("--n-informative", type=int, default=10)
    ap.add_argument("--n-redundant", type=int, default=2)
    ap.add_argument("--n-classes", type=int, default=2)
    ap.add_argument("--class-sep", type=float, default=1.0, help="Larger -> easier classification.")
    ap.add_argument("--flip-y", type=float, default=0.01, help="Label noise rate.")

    ap.add_argument("--depth", type=int, default=6)
    ap.add_argument("--mode", choices=["rerank", "replace"], default="rerank")
    ap.add_argument("--alpha", type=float, default=1.0)
    ap.add_argument("--shortlist-k", type=int, default=6)
    ap.add_argument("--thresholds", type=int, default=64)
    ap.add_argument(
        "--threshold-strategy",
        choices=["quantile", "unique_midpoints_cap", "unique_midpoints"],
        default="unique_midpoints_cap",
    )
    ap.add_argument("--reranker-target", choices=["gini", "val_gain"], default="val_gain")
    ap.add_argument("--val-fraction", type=float, default=0.25)
    ap.add_argument("--no-progress", action="store_true", help="Disable progress output.")
    args = ap.parse_args()

    runs: List[OneRun] = []

    seeds = [int(args.seed0) + i for i in range(int(args.seeds))]
    for seed in _iter_progress(seeds, desc="running seeds", enabled=(not bool(args.no_progress))):
        X, y = make_classification(
            n_samples=int(args.n_samples),
            n_features=int(args.n_features),
            n_informative=int(args.n_informative),
            n_redundant=int(args.n_redundant),
            n_classes=int(args.n_classes),
            class_sep=float(args.class_sep),
            flip_y=float(args.flip_y),
            random_state=seed,
        )
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.25, random_state=seed, stratify=y
        )

        cart = DecisionTreeClassifier(max_depth=int(args.depth), criterion="gini", random_state=seed)
        cart.fit(X_train, y_train)
        pred_cart = cart.predict(X_test)

        guided = GuidedCARTClassifier(
            max_depth=int(args.depth),
            mode=str(args.mode),
            shortlist_k=int(args.shortlist_k),
            alpha=float(args.alpha),
            max_thresholds_per_feature=int(args.thresholds),
            threshold_strategy=str(args.threshold_strategy),
            reranker_target=str(args.reranker_target),
            val_fraction=float(args.val_fraction),
            val_seed=seed,
        )
        guided.fit(X_train, y_train)
        pred_guided = guided.predict(X_test)

        acc_cart = float((pred_cart == y_test).mean())
        acc_guided = float((pred_guided == y_test).mean())
        agree = float((pred_cart == pred_guided).mean())
        disagree = float(1.0 - agree)

        runs.append(
            OneRun(
                seed=seed,
                acc_cart=acc_cart,
                acc_guided=acc_guided,
                agreement=agree,
                disagree_rate=disagree,
            )
        )

    print("=== CART vs Guided-CART: prediction agreement ===")
    print(
        "data:",
        f"n={args.n_samples}",
        f"p={args.n_features}",
        f"class_sep={args.class_sep}",
        f"flip_y={args.flip_y}",
        "| model:",
        f"depth={args.depth}",
        f"mode={args.mode}",
        f"alpha={args.alpha}",
        f"reranker_target={args.reranker_target}",
        f"threshold_strategy={args.threshold_strategy}",
        f"thresholds={args.thresholds}",
    )
    print("runs:", len(runs), "seed0:", args.seed0)

    acc_cart_mean, acc_cart_std, acc_cart_min = _summary([r.acc_cart for r in runs])
    acc_g_mean, acc_g_std, acc_g_min = _summary([r.acc_guided for r in runs])
    agree_mean, agree_std, agree_min = _summary([r.agreement for r in runs])

    print()
    print(f"acc_cart    : mean={acc_cart_mean:.4f} std={acc_cart_std:.4f} min={acc_cart_min:.4f}")
    print(f"acc_guided  : mean={acc_g_mean:.4f} std={acc_g_std:.4f} min={acc_g_min:.4f}")
    print(f"agreement   : mean={agree_mean:.4f} std={agree_std:.4f} min={agree_min:.4f}")
    print(f"disagree(%) : mean={100*(1-agree_mean):.2f}%")

    # show a few per-run lines for sanity
    print()
    print("per-run (first 5): seed acc_cart acc_guided agreement")
    for r in runs[:5]:
        print(f"- {r.seed}  {r.acc_cart:.4f}  {r.acc_guided:.4f}  {r.agreement:.4f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())


