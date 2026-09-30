from __future__ import annotations

import argparse
from typing import Dict, Optional

import numpy as np
from sklearn.datasets import make_classification
from sklearn.model_selection import train_test_split

from mtbmt.trajectory_guidance import TrajectoryRouteParams, guide_decision_tree_training


def _parse_int_or_none(s: str) -> Optional[int]:
    s = (s or "").strip()
    if s.lower() in {"none", "null", ""}:
        return None
    return int(s)


def main() -> int:
    ap = argparse.ArgumentParser(description="Trajectory-guided training demo (DecisionTreeClassifier).")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--w-effect", type=float, default=1.0)
    ap.add_argument("--w-length", type=float, default=0.05)
    ap.add_argument("--w-time", type=float, default=0.10)
    ap.add_argument("--iters", type=int, default=6, help="How many trajectory-guidance iterations to run.")
    ap.add_argument("--traj-samples", type=int, default=512, help="How many val samples used to estimate trajectory stats.")

    ap.add_argument("--init-max-depth", default="50", help="Initial max_depth (int or 'none').")
    ap.add_argument("--init-min-samples-leaf", type=int, default=2)
    ap.add_argument("--init-min-samples-split", type=int, default=5)
    args = ap.parse_args()

    rng = np.random.RandomState(args.seed)
    X, y = make_classification(
        n_samples=20000,
        n_features=30,
        n_informative=15,
        n_redundant=5,
        n_classes=5,
        random_state=args.seed,
    )
    X_train, X_tmp, y_train, y_tmp = train_test_split(X, y, test_size=0.3, random_state=args.seed)
    X_val, X_test, y_val, y_test = train_test_split(X_tmp, y_tmp, test_size=0.5, random_state=args.seed)

    route = TrajectoryRouteParams(
        w_effect=float(args.w_effect),
        w_length=float(args.w_length),
        w_time=float(args.w_time),
        n_samples_for_trajectory=int(args.traj_samples),
        max_iters=int(args.iters),
    )

    init: Dict[str, int | None] = {
        "max_depth": _parse_int_or_none(args.init_max_depth),
        "min_samples_leaf": int(args.init_min_samples_leaf),
        "min_samples_split": int(args.init_min_samples_split),
    }

    clf, params, report = guide_decision_tree_training(
        X_train=X_train,
        y_train=y_train,
        X_val=X_val,
        y_val=y_val,
        X_test=X_test,
        y_test=y_test,
        route=route,
        initial_params=init,
        random_state=int(rng.randint(0, 1_000_000)),
    )

    print("=== Trajectory-guided training result ===")
    print("best_params:", params)
    print("report:", report)
    # keep clf alive for future extension
    _ = clf
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


