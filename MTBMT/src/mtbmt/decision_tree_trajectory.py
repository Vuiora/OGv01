from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.tree import DecisionTreeClassifier


@dataclass(frozen=True)
class DecisionTreeTrajectorySummary:
    """
    从一棵 sklearn DecisionTreeClassifier 提取“轨迹”并汇总后的统计。

    - trajectory_length_mean: 平均路径长度（根->叶的步数）
    - tendency_features: 对倾向值序列（tendency）做的 mtbmt.trajectory_features.compute_trajectory_features 统计（可选）
    - retrieval_time_sec_per_sample: 提取一条轨迹的平均耗时（秒）
    """

    n_samples: int
    trajectory_length_mean: float
    retrieval_time_sec_per_sample: float
    tendency_features: Optional[Dict[str, float]]


class DecisionTreeTrajectoryExtractor:
    """
    从 sklearn 决策树中提取算法轨迹（根节点到叶子节点的路径）。

    轨迹格式与 scripts/trajectory/trajectary.py 的约定一致：
    - tendency: 每个节点的倾向值（这里用 impurity * 节点样本占比）
    - sequence: 节点深度序列
    - selection: "left"/"right"/"leaf"
    """

    def __init__(self, clf: DecisionTreeClassifier):
        self.clf = clf
        self.tree = clf.tree_

    def extract_trajectory(self, sample: np.ndarray) -> Dict[str, Any]:
        tendency: List[float] = []
        sequence: List[int] = []
        selection: List[str] = []

        node_id = 0
        depth = 0
        n_root = float(self.tree.n_node_samples[0]) if self.tree.n_node_samples[0] else 1.0

        while True:
            left_child = int(self.tree.children_left[node_id])
            right_child = int(self.tree.children_right[node_id])

            impurity = float(self.tree.impurity[node_id])
            n_samples = float(self.tree.n_node_samples[node_id])
            tendency_value = impurity * (n_samples / n_root)
            tendency.append(float(tendency_value))
            sequence.append(int(depth))

            # leaf
            if left_child == right_child:
                selection.append("leaf")
                break

            feature = int(self.tree.feature[node_id])
            threshold = float(self.tree.threshold[node_id])
            if float(sample[feature]) <= threshold:
                selection.append("left")
                node_id = left_child
            else:
                selection.append("right")
                node_id = right_child
            depth += 1

        return {"tendency": tendency, "sequence": sequence, "selection": selection}

    def extract_trajectories_batch(self, X: np.ndarray, y: Optional[np.ndarray] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for i, sample in enumerate(X):
            traj = self.extract_trajectory(sample)
            if y is not None:
                pred = self.clf.predict([sample])[0]
                traj["prediction"] = pred
                traj["is_correct"] = bool(pred == y[i])
            out.append(traj)
        return out


def summarize_decision_tree_trajectories(
    clf: DecisionTreeClassifier,
    *,
    X_samples: np.ndarray,
) -> DecisionTreeTrajectorySummary:
    from mtbmt.trajectory_features import compute_trajectory_features

    extractor = DecisionTreeTrajectoryExtractor(clf)
    t0 = time.time()
    trajs = extractor.extract_trajectories_batch(X_samples)
    elapsed = time.time() - t0

    lengths = [len(t["tendency"]) for t in trajs]
    tendency_concat: List[float] = []
    for t in trajs:
        tendency_concat.extend([float(x) for x in t["tendency"]])

    tf = compute_trajectory_features(tendency_concat, normalize=True)
    tf_dict = None if tf is None else {k: float(v) for k, v in tf.as_dict().items()}

    n = int(len(trajs))
    return DecisionTreeTrajectorySummary(
        n_samples=n,
        trajectory_length_mean=float(np.mean(lengths)) if lengths else 0.0,
        retrieval_time_sec_per_sample=float(elapsed / max(n, 1)),
        tendency_features=tf_dict,
    )


def decision_tree_objective(
    *,
    decision_effect: float,
    trajectory_length_mean: float,
    retrieval_time_sec_per_sample: float,
    w_effect: float = 1.0,
    w_length: float = 0.05,
    w_time: float = 0.10,
) -> float:
    """
    一个简单的“优质轨迹路线”打分：
    - 更高 decision_effect 更好
    - 更短 trajectory_length 更好（惩罚）
    - 更小 retrieval_time 更好（惩罚）
    """

    return (
        float(w_effect) * float(decision_effect)
        - float(w_length) * float(trajectory_length_mean)
        - float(w_time) * float(np.log1p(max(float(retrieval_time_sec_per_sample), 0.0)))
    )


