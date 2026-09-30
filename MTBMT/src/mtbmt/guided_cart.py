from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from sklearn.ensemble import RandomForestClassifier


def _gini(y: np.ndarray) -> float:
    if y.size == 0:
        return 0.0
    _, counts = np.unique(y, return_counts=True)
    p = counts.astype(float) / float(y.size)
    return float(1.0 - (p * p).sum())


def _majority_class(y: np.ndarray) -> int:
    vals, counts = np.unique(y, return_counts=True)
    return int(vals[int(np.argmax(counts))])

def _accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return 0.0
    return float((y_true == y_pred).mean())


@dataclass(frozen=True)
class SplitCandidate:
    feature_idx: int
    threshold: float
    gain_gini: float
    left_ratio: float
    feat_mean: float
    feat_std: float


@dataclass(frozen=True)
class NodeState:
    depth: int
    n_samples: int
    n_classes: int
    node_gini: float


def _node_state(depth: int, y: np.ndarray) -> NodeState:
    return NodeState(
        depth=int(depth),
        n_samples=int(y.size),
        n_classes=int(np.unique(y).size),
        node_gini=_gini(y),
    )


def _thresholds_for_feature(
    x: np.ndarray,
    *,
    max_thresholds: int = 16,
    strategy: str = "quantile",
) -> np.ndarray:
    """
    生成连续特征候选阈值。

    - quantile: 使用分位点近似（更快，但可能偏离 sklearn CART 的“扫描所有候选切分点”口径）
    - unique_midpoints: 用排序后的 unique 值相邻中点（更接近 sklearn，但可能更慢）
    - unique_midpoints_cap: 同 unique_midpoints，但将中点数量压到 max_thresholds（推荐折中）
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
        # cap
        k = max(2, int(max_thresholds))
        if mids.size <= k:
            return np.unique(mids)
        idx = np.linspace(0, mids.size - 1, num=k).round().astype(int)
        return np.unique(mids[idx])

    # default: quantile
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
    max_thresholds_per_feature: int = 16,
    min_samples_leaf: int = 1,
    threshold_strategy: str = "quantile",
) -> List[SplitCandidate]:
    """
    CART（分类）候选分裂：用 gini gain 作为 baseline 评价。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y)
    n, p = X.shape
    g_parent = _gini(y)
    cands: List[SplitCandidate] = []

    for j in range(p):
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
            gain = g_parent - (n_left / n) * _gini(y_left) - (n_right / n) * _gini(y_right)
            cands.append(
                SplitCandidate(
                    feature_idx=int(j),
                    threshold=float(thr),
                    gain_gini=float(gain),
                    left_ratio=float(n_left / n),
                    feat_mean=mean_j,
                    feat_std=std_j,
                )
            )
    return cands


def candidate_feature_row(state: NodeState, cand: SplitCandidate) -> Dict[str, float]:
    return {
        "depth": float(state.depth),
        "n_samples": float(state.n_samples),
        "n_classes": float(state.n_classes),
        "node_gini": float(state.node_gini),
        "feature_idx": float(cand.feature_idx),
        "threshold": float(cand.threshold),
        "gain_gini": float(cand.gain_gini),
        "left_ratio": float(cand.left_ratio),
        "feat_mean": float(cand.feat_mean),
        "feat_std": float(cand.feat_std),
    }

def _val_gain_for_candidate(
    *,
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_val: np.ndarray,
    y_val: np.ndarray,
    cand: SplitCandidate,
) -> float:
    """
    用“一个 split + 子节点多数类预测”近似评估候选分裂的验证集即时收益。

    baseline: 父节点多数类在 val 上的准确率
    after:    左/右子节点分别用其在 train 子集的多数类来预测 val
    reward:   after - baseline
    """
    if y_val.size == 0:
        return 0.0

    parent_pred = _majority_class(y_train)
    base = _accuracy(y_val, np.full_like(y_val, parent_pred))

    j = int(cand.feature_idx)
    thr = float(cand.threshold)

    # split train -> determine child predictors
    mask_tr_left = X_train[:, j] <= thr
    y_tr_left = y_train[mask_tr_left]
    y_tr_right = y_train[~mask_tr_left]
    if y_tr_left.size == 0 or y_tr_right.size == 0:
        return float("-inf")
    pred_left = _majority_class(y_tr_left)
    pred_right = _majority_class(y_tr_right)

    # apply to val
    mask_va_left = X_val[:, j] <= thr
    y_va_left = y_val[mask_va_left]
    y_va_right = y_val[~mask_va_left]
    after = 0.0
    if y_va_left.size:
        after += (y_va_left.size / y_val.size) * _accuracy(y_va_left, np.full_like(y_va_left, pred_left))
    if y_va_right.size:
        after += (y_va_right.size / y_val.size) * _accuracy(y_va_right, np.full_like(y_va_right, pred_right))

    return float(after - base)


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
    CART 节点候选分裂重排器：
    - 训练标签来自“标准 CART（gini gain 最大）”在该节点会选择的候选
    - 预测时输出 P(chosen) 用于重排/替换
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
        if proba.shape[1] == 2:
            return proba[:, 1]
        return np.ones((X.shape[0],), dtype=float) / float(max(X.shape[0], 1))


def collect_cart_split_training_data(
    X: np.ndarray,
    y: np.ndarray,
    *,
    X_val: Optional[np.ndarray] = None,
    y_val: Optional[np.ndarray] = None,
    max_depth: int = 8,
    min_samples_split: int = 2,
    min_samples_leaf: int = 1,
    max_thresholds_per_feature: int = 16,
    threshold_strategy: str = "quantile",
    target: str = "gini",  # gini|val_gain
    max_val_samples_per_node: int = 1024,
) -> Tuple[List[Dict[str, float]], List[int]]:
    """
    从“标准 CART（gini gain 最大）”建树过程中收集训练样本：
    每个节点：对所有候选分裂生成一行特征，y=1 对应被 CART 选中的候选，其他为 0。
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y)
    Xv = None if X_val is None else np.asarray(X_val, dtype=float)
    yv = None if y_val is None else np.asarray(y_val)

    X_rows: List[Dict[str, float]] = []
    y_rows: List[int] = []

    def _recurse(Xn: np.ndarray, yn: np.ndarray, Xvn: Optional[np.ndarray], yvn: Optional[np.ndarray], depth: int) -> None:
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

        tgt = (target or "gini").strip().lower()
        if tgt == "val_gain":
            if Xvn is None or yvn is None:
                raise ValueError("target=val_gain requires X_val/y_val")
            # cap val samples per node for speed (deterministic slice)
            if yvn.size > int(max_val_samples_per_node):
                Xvn2 = Xvn[: int(max_val_samples_per_node)]
                yvn2 = yvn[: int(max_val_samples_per_node)]
            else:
                Xvn2, yvn2 = Xvn, yvn
            gains = []
            for c in cands:
                vg = _val_gain_for_candidate(X_train=Xn, y_train=yn, X_val=Xvn2, y_val=yvn2, cand=c)
                gains.append(vg)
            gains = np.asarray(gains, dtype=float)
            # tie-break by gini gain
            gg = np.asarray([c.gain_gini for c in cands], dtype=float)
            best_idx = int(np.lexsort((-gg, -gains))[0])
        else:
            best_idx = int(np.argmax([c.gain_gini for c in cands]))

        for i, c in enumerate(cands):
            X_rows.append(candidate_feature_row(state, c))
            y_rows.append(1 if i == best_idx else 0)

        best = cands[best_idx]
        mask_left = Xn[:, best.feature_idx] <= best.threshold
        if Xvn is not None and yvn is not None:
            mask_v_left = Xvn[:, best.feature_idx] <= best.threshold
            _recurse(Xn[mask_left], yn[mask_left], Xvn[mask_v_left], yvn[mask_v_left], depth + 1)
            _recurse(Xn[~mask_left], yn[~mask_left], Xvn[~mask_v_left], yvn[~mask_v_left], depth + 1)
        else:
            _recurse(Xn[mask_left], yn[mask_left], None, None, depth + 1)
            _recurse(Xn[~mask_left], yn[~mask_left], None, None, depth + 1)

    _recurse(X, y, Xv, yv, 0)
    return X_rows, y_rows


class GuidedCARTClassifier:
    """
    Guided-CART（分类）：每个节点先生成候选分裂（CART/gini），再用倾向模型重排/替换，
    弱化/绕开“基尼指数”作为唯一决策依据。
    """

    def __init__(
        self,
        *,
        max_depth: int = 8,
        min_samples_split: int = 2,
        min_samples_leaf: int = 1,
        max_thresholds_per_feature: int = 16,
        threshold_strategy: str = "quantile",
        reranker_target: str = "gini",  # gini|val_gain
        val_fraction: float = 0.25,
        val_seed: int = 0,
        max_val_samples_per_node: int = 1024,
        mode: str = "rerank",  # rerank|replace
        shortlist_k: int = 8,
        alpha: float = 1.0,
        reranker: Optional[SplitReranker] = None,
    ):
        self.max_depth = int(max_depth)
        self.min_samples_split = int(min_samples_split)
        self.min_samples_leaf = int(min_samples_leaf)
        self.max_thresholds_per_feature = int(max_thresholds_per_feature)
        self.threshold_strategy = str(threshold_strategy)
        self.reranker_target = str(reranker_target)
        self.val_fraction = float(val_fraction)
        self.val_seed = int(val_seed)
        self.max_val_samples_per_node = int(max_val_samples_per_node)
        self.mode = str(mode).lower()
        self.shortlist_k = int(shortlist_k)
        self.alpha = float(alpha)
        self.reranker = reranker
        self.root_: Optional[_TreeNode] = None

    def fit_reranker_from_cart(self, X: np.ndarray, y: np.ndarray, *, random_state: int = 0) -> "GuidedCARTClassifier":
        # deterministic split for val_gain target
        X = np.asarray(X, dtype=float)
        y = np.asarray(y)
        if self.reranker_target.strip().lower() == "val_gain":
            rng = np.random.RandomState(self.val_seed)
            n = int(len(X))
            idx = rng.permutation(n)
            n_val = int(max(1, round(self.val_fraction * n)))
            val_idx = idx[:n_val]
            tr_idx = idx[n_val:]
            X_tr, y_tr = X[tr_idx], y[tr_idx]
            X_val, y_val = X[val_idx], y[val_idx]
        else:
            X_tr, y_tr, X_val, y_val = X, y, None, None

        rows, yy = collect_cart_split_training_data(
            X_tr,
            y_tr,
            X_val=X_val,
            y_val=y_val,
            max_depth=self.max_depth,
            min_samples_split=self.min_samples_split,
            min_samples_leaf=self.min_samples_leaf,
            max_thresholds_per_feature=self.max_thresholds_per_feature,
            threshold_strategy=self.threshold_strategy,
            target=self.reranker_target,
            max_val_samples_per_node=self.max_val_samples_per_node,
        )
        rr = SplitReranker(random_state=random_state)
        rr.fit(rows, yy)
        self.reranker = rr
        return self

    def fit(self, X: np.ndarray, y: np.ndarray) -> "GuidedCARTClassifier":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y)
        if self.reranker is None:
            self.fit_reranker_from_cart(X, y)

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
                threshold_strategy=self.threshold_strategy,
            )
            if not cands:
                return _TreeNode(is_leaf=True, pred_class=pred, depth=depth)

            gains = np.asarray([c.gain_gini for c in cands], dtype=float)
            order = np.argsort(-gains)
            if self.mode == "rerank":
                keep = order[: max(1, min(self.shortlist_k, order.size))]
                cands2 = [cands[i] for i in keep]
            else:
                cands2 = cands

            rows = [candidate_feature_row(state, c) for c in cands2]
            proba = self.reranker.score_candidates(rows) if self.reranker is not None else np.zeros((len(rows),), dtype=float)

            gains2 = np.asarray([c.gain_gini for c in cands2], dtype=float)
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
            raise ValueError("GuidedCARTClassifier not fitted")
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

    def split_signatures_preorder(self) -> List[Tuple[int, int, float]]:
        """
        用于调试/对比：以先序遍历返回每个内部节点的 (depth, feature_idx, threshold)。
        """
        if self.root_ is None:
            return []
        out: List[Tuple[int, int, float]] = []

        def _walk(node: Optional[_TreeNode]) -> None:
            if node is None or node.is_leaf:
                return
            out.append((int(node.depth), int(node.feature_idx or 0), float(node.threshold or 0.0)))
            _walk(node.left)
            _walk(node.right)

        _walk(self.root_)
        return out


