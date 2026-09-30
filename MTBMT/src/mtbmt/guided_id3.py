from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.ensemble import RandomForestClassifier


def _entropy(y: np.ndarray) -> float:
    if y.size == 0:
        return 0.0
    _, counts = np.unique(y, return_counts=True)
    p = counts.astype(float) / float(y.size)
    p = p[p > 0]
    return float(-(p * np.log2(p)).sum())


def _gini(y: np.ndarray) -> float:
    if y.size == 0:
        return 0.0
    _, counts = np.unique(y, return_counts=True)
    p = counts.astype(float) / float(y.size)
    return float(1.0 - (p * p).sum())


def _majority_class(y: np.ndarray) -> int:
    vals, counts = np.unique(y, return_counts=True)
    return int(vals[int(np.argmax(counts))])


@dataclass(frozen=True)
class SplitCandidate:
    feature_idx: int
    threshold: float
    gain_entropy: float
    gain_gini: float
    left_ratio: float
    feat_mean: float
    feat_std: float


@dataclass(frozen=True)
class NodeState:
    depth: int
    n_samples: int
    n_classes: int
    entropy: float
    gini: float


def _node_state(depth: int, y: np.ndarray) -> NodeState:
    return NodeState(
        depth=int(depth),
        n_samples=int(y.size),
        n_classes=int(np.unique(y).size),
        entropy=_entropy(y),
        gini=_gini(y),
    )


def _thresholds_for_feature(
    x: np.ndarray,
    *,
    max_thresholds: int = 16,
    strategy: str = "quantile",
) -> np.ndarray:
    """
    为连续特征生成候选阈值。

    - quantile: 分位点近似（更快）
    - unique_midpoints: 相邻 unique 值的中点（更接近“扫描所有切分点”）
    - unique_midpoints_cap: 中点数量压到 max_thresholds（推荐折中）
    """
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    if x.size < 2:
        return np.asarray([], dtype=float)

    strat = (strategy or "quantile").strip().lower()
    if strat in {"unique_midpoints", "unique_midpoints_cap"}:
        vals = np.unique(x)
        if vals.size < 2:
            return np.asarray([], dtype=float)
        mids = (vals[:-1] + vals[1:]) / 2.0
        if mids.size == 0:
            return np.asarray([], dtype=float)
        if strat == "unique_midpoints":
            return np.unique(mids)
        k = max(2, int(max_thresholds))
        if mids.size <= k:
            return np.unique(mids)
        idx = np.linspace(0, mids.size - 1, num=k).round().astype(int)
        return np.unique(mids[idx])

    qs = np.linspace(0.05, 0.95, num=max(2, int(max_thresholds)))
    cuts = np.unique(np.quantile(x, qs))
    if cuts.size == 0:
        return np.asarray([], dtype=float)
    cuts = np.unique(cuts)
    if cuts.size == 1:
        return cuts
    mids = (cuts[:-1] + cuts[1:]) / 2.0
    return np.unique(mids)


def generate_split_candidates(
    X: np.ndarray,
    y: np.ndarray,
    *,
    depth: int,
    max_features: Optional[int] = None,
    max_thresholds_per_feature: int = 16,
    min_samples_leaf: int = 1,
    threshold_strategy: str = "quantile",
) -> List[SplitCandidate]:
    """
    在当前节点生成候选分裂（连续特征二分）。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y)
    n, p = X.shape
    if max_features is None:
        feature_indices = range(p)
    else:
        feature_indices = range(min(int(max_features), p))

    h_parent = _entropy(y)
    g_parent = _gini(y)

    cands: List[SplitCandidate] = []
    for j in feature_indices:
        xj = X[:, j]
        if not np.isfinite(xj).any():
            continue
        thresholds = _thresholds_for_feature(
            xj,
            max_thresholds=max_thresholds_per_feature,
            strategy=threshold_strategy,
        )
        if thresholds.size == 0:
            continue
        mean_j = float(np.nanmean(xj))
        std_j = float(np.nanstd(xj))
        for thr in thresholds:
            mask_left = xj <= thr
            n_left = int(mask_left.sum())
            n_right = int(n - n_left)
            if n_left < int(min_samples_leaf) or n_right < int(min_samples_leaf):
                continue
            y_left = y[mask_left]
            y_right = y[~mask_left]
            h = h_parent - (n_left / n) * _entropy(y_left) - (n_right / n) * _entropy(y_right)
            g = g_parent - (n_left / n) * _gini(y_left) - (n_right / n) * _gini(y_right)
            cands.append(
                SplitCandidate(
                    feature_idx=int(j),
                    threshold=float(thr),
                    gain_entropy=float(h),
                    gain_gini=float(g),
                    left_ratio=float(n_left / n),
                    feat_mean=mean_j,
                    feat_std=std_j,
                )
            )
    return cands


def candidate_feature_row(state: NodeState, cand: SplitCandidate) -> Dict[str, float]:
    """
    倾向模型的输入特征：节点状态 + 候选分裂的属性。
    """
    return {
        # state
        "depth": float(state.depth),
        "n_samples": float(state.n_samples),
        "n_classes": float(state.n_classes),
        "node_entropy": float(state.entropy),
        "node_gini": float(state.gini),
        # candidate
        "feature_idx": float(cand.feature_idx),
        "threshold": float(cand.threshold),
        "gain_entropy": float(cand.gain_entropy),
        "gain_gini": float(cand.gain_gini),
        "left_ratio": float(cand.left_ratio),
        "feat_mean": float(cand.feat_mean),
        "feat_std": float(cand.feat_std),
    }


@dataclass
class _TreeNode:
    is_leaf: bool
    pred_class: int
    feature_idx: Optional[int] = None
    threshold: Optional[float] = None
    left: Optional["_TreeNode"] = None
    right: Optional["_TreeNode"] = None
    depth: int = 0


class SplitReranker:
    """
    候选分裂重排器（倾向模型）。

    训练方式：从标准 ID3（信息增益）生成的“节点候选-被选中”样本上训练二分类器：
    - y=1 表示该候选是标准 ID3 会选的分裂
    - 预测时对每个候选输出 P(chosen)，作为重排分数
    """

    def __init__(self, *, random_state: int = 0, n_estimators: int = 300):
        self.model = RandomForestClassifier(
            n_estimators=int(n_estimators),
            random_state=int(random_state),
            n_jobs=-1,
            class_weight="balanced",
        )
        self.columns: Optional[List[str]] = None

    def fit(self, X_rows: List[Dict[str, float]], y: List[int]) -> "SplitReranker":
        if not X_rows:
            raise ValueError("empty training rows for SplitReranker")
        cols = sorted({k for r in X_rows for k in r.keys()})
        X = np.asarray([[r.get(c, np.nan) for c in cols] for r in X_rows], dtype=float)
        yv = np.asarray(y, dtype=int)
        # 简单缺失填充：用列中位数（避免引入额外依赖）
        med = np.nanmedian(X, axis=0)
        idx = np.where(~np.isfinite(X))
        X[idx] = med[idx[1]]
        self.model.fit(X, yv)
        self.columns = cols
        return self

    def score_candidates(self, rows: List[Dict[str, float]]) -> np.ndarray:
        if self.columns is None:
            raise ValueError("SplitReranker not fitted")
        cols = self.columns
        X = np.asarray([[r.get(c, np.nan) for c in cols] for r in rows], dtype=float)
        med = np.nanmedian(X, axis=0)
        idx = np.where(~np.isfinite(X))
        X[idx] = med[idx[1]]
        proba = self.model.predict_proba(X)
        # positive class prob
        if proba.shape[1] == 2:
            return proba[:, 1]
        # degenerate fallback
        return np.ones((X.shape[0],), dtype=float) / float(max(X.shape[0], 1))


def collect_id3_split_training_data(
    X: np.ndarray,
    y: np.ndarray,
    *,
    max_depth: int = 8,
    min_samples_split: int = 2,
    min_samples_leaf: int = 1,
    max_thresholds_per_feature: int = 16,
    threshold_strategy: str = "quantile",
) -> Tuple[List[Dict[str, float]], List[int]]:
    """
    从“标准 ID3（信息增益最大）”建树过程中收集训练样本：
    每个节点：对所有候选分裂生成一行特征，y=1 对应被 ID3 选中的候选，其他为 0。
    """

    X = np.asarray(X, dtype=float)
    y = np.asarray(y)

    X_rows: List[Dict[str, float]] = []
    y_rows: List[int] = []

    def _recurse(Xn: np.ndarray, yn: np.ndarray, depth: int) -> None:
        if depth >= int(max_depth) or yn.size < int(min_samples_split) or np.unique(yn).size <= 1:
            return
        state = _node_state(depth, yn)
        cands = generate_split_candidates(
            Xn,
            yn,
            depth=depth,
            max_thresholds_per_feature=max_thresholds_per_feature,
            min_samples_leaf=min_samples_leaf,
            threshold_strategy=threshold_strategy,
        )
        if not cands:
            return
        # standard ID3 pick: max entropy gain
        best_idx = int(np.argmax([c.gain_entropy for c in cands]))
        for i, c in enumerate(cands):
            X_rows.append(candidate_feature_row(state, c))
            y_rows.append(1 if i == best_idx else 0)

        best = cands[best_idx]
        mask_left = Xn[:, best.feature_idx] <= best.threshold
        _recurse(Xn[mask_left], yn[mask_left], depth + 1)
        _recurse(Xn[~mask_left], yn[~mask_left], depth + 1)

    _recurse(X, y, 0)
    return X_rows, y_rows


class GuidedID3Classifier:
    """
    Guided-ID3：每个节点先生成候选分裂，再用“倾向模型”对候选进行重排/替换，
    从而弱化/绕开传统的分裂准则（如信息增益/基尼指数）。
    """

    def __init__(
        self,
        *,
        max_depth: int = 8,
        min_samples_split: int = 2,
        min_samples_leaf: int = 1,
        max_thresholds_per_feature: int = 16,
        mode: str = "rerank",  # rerank|replace
        shortlist_k: int = 8,
        alpha: float = 1.0,  # score = alpha*proba + (1-alpha)*norm_gain
        reranker: Optional[SplitReranker] = None,
    ):
        self.max_depth = int(max_depth)
        self.min_samples_split = int(min_samples_split)
        self.min_samples_leaf = int(min_samples_leaf)
        self.max_thresholds_per_feature = int(max_thresholds_per_feature)
        self.mode = str(mode).lower()
        self.shortlist_k = int(shortlist_k)
        self.alpha = float(alpha)
        self.reranker = reranker
        self.root_: Optional[_TreeNode] = None

    def fit_reranker_from_id3(self, X: np.ndarray, y: np.ndarray, *, random_state: int = 0) -> "GuidedID3Classifier":
        rows, yy = collect_id3_split_training_data(
            X,
            y,
            max_depth=self.max_depth,
            min_samples_split=self.min_samples_split,
            min_samples_leaf=self.min_samples_leaf,
            max_thresholds_per_feature=self.max_thresholds_per_feature,
        )
        rr = SplitReranker(random_state=random_state)
        rr.fit(rows, yy)
        self.reranker = rr
        return self

    def fit(self, X: np.ndarray, y: np.ndarray) -> "GuidedID3Classifier":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y)
        if self.reranker is None:
            # 默认：先用标准 ID3 的决策数据训练倾向模型（可用外部注入替代）
            self.fit_reranker_from_id3(X, y)

        def _build(Xn: np.ndarray, yn: np.ndarray, depth: int) -> _TreeNode:
            pred = _majority_class(yn) if yn.size else 0
            if depth >= self.max_depth or yn.size < self.min_samples_split or np.unique(yn).size <= 1:
                return _TreeNode(is_leaf=True, pred_class=pred, depth=depth)

            state = _node_state(depth, yn)
            cands = generate_split_candidates(
                Xn,
                yn,
                depth=depth,
                max_thresholds_per_feature=self.max_thresholds_per_feature,
                min_samples_leaf=self.min_samples_leaf,
            )
            if not cands:
                return _TreeNode(is_leaf=True, pred_class=pred, depth=depth)

            # baseline shortlist by entropy gain (still helps stability)
            gains = np.asarray([c.gain_entropy for c in cands], dtype=float)
            order = np.argsort(-gains)
            if self.mode == "rerank":
                keep = order[: max(1, min(self.shortlist_k, order.size))]
                cands2 = [cands[i] for i in keep]
            else:
                cands2 = cands

            rows = [candidate_feature_row(state, c) for c in cands2]
            proba = self.reranker.score_candidates(rows) if self.reranker is not None else np.zeros((len(rows),), dtype=float)

            # mix with normalized gain (optional)
            gains2 = np.asarray([c.gain_entropy for c in cands2], dtype=float)
            if np.allclose(gains2.max(), gains2.min()):
                gain_norm = np.zeros_like(gains2)
            else:
                gain_norm = (gains2 - gains2.min()) / (gains2.max() - gains2.min() + 1e-12)

            score = self.alpha * proba + (1.0 - self.alpha) * gain_norm
            best_i = int(np.argmax(score))
            best = cands2[best_i]

            mask_left = Xn[:, best.feature_idx] <= best.threshold
            left = _build(Xn[mask_left], yn[mask_left], depth + 1)
            right = _build(Xn[~mask_left], yn[~mask_left], depth + 1)
            return _TreeNode(
                is_leaf=False,
                pred_class=pred,
                feature_idx=int(best.feature_idx),
                threshold=float(best.threshold),
                left=left,
                right=right,
                depth=depth,
            )

        self.root_ = _build(X, y, 0)
        return self

    def predict(self, X: np.ndarray) -> np.ndarray:
        if self.root_ is None:
            raise ValueError("GuidedID3Classifier not fitted")
        X = np.asarray(X, dtype=float)

        def _pred_one(x: np.ndarray) -> int:
            node = self.root_
            while node is not None and not node.is_leaf:
                j = int(node.feature_idx or 0)
                thr = float(node.threshold or 0.0)
                if float(x[j]) <= thr:
                    node = node.left
                else:
                    node = node.right
            return int(node.pred_class if node is not None else 0)

        return np.asarray([_pred_one(x) for x in X], dtype=int)

    def tree_stats(self) -> Dict[str, Any]:
        if self.root_ is None:
            return {"n_nodes": 0, "max_depth": 0}

        n_nodes = 0
        max_depth = 0

        stack = [self.root_]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            n_nodes += 1
            max_depth = max(max_depth, int(node.depth))
            if not node.is_leaf:
                stack.append(node.left)
                stack.append(node.right)
        return {"n_nodes": int(n_nodes), "max_depth": int(max_depth)}


