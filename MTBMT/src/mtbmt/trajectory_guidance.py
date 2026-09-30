from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np
from sklearn.tree import DecisionTreeClassifier

from mtbmt.decision_tree_trajectory import (
    DecisionTreeTrajectorySummary,
    decision_tree_objective,
    summarize_decision_tree_trajectories,
)


@dataclass(frozen=True)
class TrajectoryRouteParams:
    """
    你提供的“更优质学习路线”偏好参数。

    - w_effect: 更看重最终效果（如准确率/收益）
    - w_length: 更看重轨迹更短（更简单/更可控）
    - w_time: 更看重检索/提取轨迹时间更低（更快）
    - n_samples_for_trajectory: 用多少样本估计轨迹统计（越大越稳、越慢）
    - max_iters: 轨迹纠偏的迭代次数（越大越慢、越可能更优）
    """

    w_effect: float = 1.0
    w_length: float = 0.05
    w_time: float = 0.10

    n_samples_for_trajectory: int = 512
    max_iters: int = 6


def _clip_int(v: int, lo: int, hi: int) -> int:
    return int(max(lo, min(int(v), hi)))


def _next_tree_params(
    *,
    current: Dict[str, int | None],
    summary: DecisionTreeTrajectorySummary,
    route: TrajectoryRouteParams,
    last_effect: float,
) -> Dict[str, int | None]:
    """
    一个非常轻量的“纠偏器”：
    - 轨迹过长 -> 降低 max_depth / 提高 min_samples_leaf
    - 效果偏低但轨迹很短 -> 放宽 max_depth / 降低 min_samples_leaf
    """

    max_depth = current.get("max_depth", None)
    min_samples_leaf = int(current.get("min_samples_leaf", 1) or 1)
    min_samples_split = int(current.get("min_samples_split", 2) or 2)

    # Heuristics thresholds (可按需求参数化)
    too_long = summary.trajectory_length_mean > 18
    very_short = summary.trajectory_length_mean < 6
    low_effect = last_effect < 0.65

    if too_long:
        # shrink complexity
        if max_depth is None:
            max_depth = 30
        max_depth = _clip_int(int(max_depth) - 5, 2, 100)
        min_samples_leaf = _clip_int(min_samples_leaf + 1, 1, 5000)
        min_samples_split = _clip_int(min_samples_split + 2, 2, 5000)
    elif low_effect and very_short:
        # allow more complexity
        if max_depth is None:
            max_depth = 20
        max_depth = _clip_int(int(max_depth) + 5, 2, 200)
        min_samples_leaf = _clip_int(min_samples_leaf - 1, 1, 5000)
        min_samples_split = _clip_int(min_samples_split - 2, 2, 5000)
    elif low_effect:
        # mild relax
        if max_depth is None:
            max_depth = 25
        max_depth = _clip_int(int(max_depth) + 2, 2, 200)
        min_samples_leaf = _clip_int(min_samples_leaf, 1, 5000)
        min_samples_split = _clip_int(min_samples_split, 2, 5000)
    else:
        # mild regularize
        if max_depth is not None:
            max_depth = _clip_int(int(max_depth), 2, 200)

    return {
        "max_depth": max_depth,
        "min_samples_leaf": min_samples_leaf,
        "min_samples_split": min_samples_split,
    }


def guide_decision_tree_training(
    *,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    X_test: np.ndarray,
    y_test: np.ndarray,
    route: TrajectoryRouteParams,
    initial_params: Optional[Dict[str, int | None]] = None,
    random_state: int = 0,
) -> Tuple[DecisionTreeClassifier, Dict[str, int | None], Dict[str, float]]:
    """
    给定一个目标学习算法（这里以 DecisionTreeClassifier 为例），
    通过“轨迹统计 + 用户偏好参数(route)”迭代纠偏其训练超参。

    返回：
    - clf: 最终选择的模型
    - params: 最终超参
    - report: 关键指标（decision_effect/trajectory_length/time/objective）
    """

    params: Dict[str, int | None] = dict(initial_params or {})
    params.setdefault("max_depth", 50)
    params.setdefault("min_samples_leaf", 2)
    params.setdefault("min_samples_split", 5)

    best_obj = float("-inf")
    best: Optional[Tuple[DecisionTreeClassifier, Dict[str, int | None], Dict[str, float]]] = None

    n_traj = int(route.n_samples_for_trajectory)
    n_traj = max(32, min(n_traj, int(len(X_val))))
    X_traj = X_val[:n_traj]

    for _ in range(int(route.max_iters)):
        clf = DecisionTreeClassifier(
            max_depth=params.get("max_depth", None),
            min_samples_leaf=int(params.get("min_samples_leaf", 1) or 1),
            min_samples_split=int(params.get("min_samples_split", 2) or 2),
            random_state=random_state,
        )
        clf.fit(X_train, y_train)

        # decision_effect: 用 test accuracy 作为示例（也可换成 AUC/R2 等）
        effect = float(clf.score(X_test, y_test))

        # trajectory summary: 用验证集样本估计
        summ = summarize_decision_tree_trajectories(clf, X_samples=X_traj)

        obj = decision_tree_objective(
            decision_effect=effect,
            trajectory_length_mean=summ.trajectory_length_mean,
            retrieval_time_sec_per_sample=summ.retrieval_time_sec_per_sample,
            w_effect=route.w_effect,
            w_length=route.w_length,
            w_time=route.w_time,
        )

        report = {
            "decision_effect": effect,
            "trajectory_length_mean": float(summ.trajectory_length_mean),
            "retrieval_time_sec_per_sample": float(summ.retrieval_time_sec_per_sample),
            "objective": float(obj),
        }

        if obj > best_obj:
            best_obj = obj
            best = (clf, dict(params), report)

        # 下一步：根据“当前轨迹偏离”调整超参
        params = _next_tree_params(current=params, summary=summ, route=route, last_effect=effect)

    assert best is not None
    return best


