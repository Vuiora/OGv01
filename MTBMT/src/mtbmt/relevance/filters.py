from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
from sklearn.feature_selection import mutual_info_classif, mutual_info_regression

from .base import BaseRelevanceScorer, RelevanceResult, _ensure_feature_names


def _nan_safe_center(x: np.ndarray) -> np.ndarray:
    # 简单缺失处理：按列均值填充（用于相关类方法；更复杂策略应在上游完成）
    x2 = x.copy()
    if np.isnan(x2).any():
        col_mean = np.nanmean(x2, axis=0)
        inds = np.where(np.isnan(x2))
        x2[inds] = np.take(col_mean, inds[1])
    return x2


def _pearson_abs(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    X = _nan_safe_center(X)
    y = y.astype(float)
    y = y - np.mean(y)
    Xc = X - np.mean(X, axis=0)
    denom = (np.std(Xc, axis=0) + 1e-12) * (np.std(y) + 1e-12)
    corr = (Xc * y[:, None]).mean(axis=0) / denom
    return np.abs(corr)


def _rankdata_1d(a: np.ndarray) -> np.ndarray:
    # 轻量替代 scipy.stats.rankdata：平均秩
    order = np.argsort(a, kind="mergesort")
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(a) + 1, dtype=float)
    # ties -> average rank
    sorted_a = a[order]
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and sorted_a[j + 1] == sorted_a[i]:
            j += 1
        if j > i:
            avg = (i + 1 + j + 1) / 2.0
            ranks[order[i : j + 1]] = avg
        i = j + 1
    return ranks


def _spearman_abs(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    X = _nan_safe_center(X)
    yr = _rankdata_1d(y)
    Xr = np.apply_along_axis(_rankdata_1d, 0, X)
    return _pearson_abs(Xr, yr)


def _distance_correlation(X: np.ndarray, y: np.ndarray) -> np.ndarray:
    """
    距离相关（dCor）的一维y版本：对每一列 X_j 计算 dCor(X_j, y)。
    该实现为 O(n^2 * p) 的朴素版本，适合中小样本；大样本建议抽样或近似。
    """
    X = _nan_safe_center(X)
    y = y.astype(float)
    n = X.shape[0]

    def _dcov_from_dist(A: np.ndarray, B: np.ndarray) -> float:
        # 双中心化
        A = A - A.mean(axis=0, keepdims=True) - A.mean(axis=1, keepdims=True) + A.mean()
        B = B - B.mean(axis=0, keepdims=True) - B.mean(axis=1, keepdims=True) + B.mean()
        return np.sqrt(np.maximum((A * B).sum() / (n * n), 0.0))

    # y 距离矩阵
    dy = np.abs(y[:, None] - y[None, :])
    dcyy = _dcov_from_dist(dy, dy) + 1e-12

    out = np.zeros(X.shape[1], dtype=float)
    for j in range(X.shape[1]):
        xj = X[:, j].astype(float)
        dx = np.abs(xj[:, None] - xj[None, :])
        dcxx = _dcov_from_dist(dx, dx) + 1e-12
        dcxy = _dcov_from_dist(dx, dy)
        out[j] = dcxy / np.sqrt(dcxx * dcyy)
    return out


@dataclass
class PearsonAbsScorer(BaseRelevanceScorer):
    name: str = "pearson_abs"

    def fit_score(self, X: np.ndarray, y: np.ndarray, feature_names: Optional[Iterable[str]] = None) -> RelevanceResult:
        t0 = time.perf_counter()
        scores = _pearson_abs(X, y)
        dt = time.perf_counter() - t0
        names = _ensure_feature_names(feature_names, X.shape[1])
        return RelevanceResult(names, scores, meta={"runtime_sec": dt})


@dataclass
class SpearmanAbsScorer(BaseRelevanceScorer):
    name: str = "spearman_abs"

    def fit_score(self, X: np.ndarray, y: np.ndarray, feature_names: Optional[Iterable[str]] = None) -> RelevanceResult:
        t0 = time.perf_counter()
        scores = _spearman_abs(X, y)
        dt = time.perf_counter() - t0
        names = _ensure_feature_names(feature_names, X.shape[1])
        return RelevanceResult(names, scores, meta={"runtime_sec": dt})


@dataclass
class MutualInfoScorer(BaseRelevanceScorer):
    """
    互信息（MI）量化：
    - 分类：mutual_info_classif
    - 回归：mutual_info_regression

    注意：MI 对离散/连续、尺度、噪声较敏感；建议配合稳定性指标与重复采样。
    """

    task: str = "auto"  # "classification" | "regression" | "auto"
    n_neighbors: int = 3
    random_state: int = 0

    @property
    def name(self) -> str:  # type: ignore[override]
        # 用稳定的“算法族”名称，避免因为超参数不同导致经验库 label 破碎。
        return "mutual_info"

    def fit_score(self, X: np.ndarray, y: np.ndarray, feature_names: Optional[Iterable[str]] = None) -> RelevanceResult:
        t0 = time.perf_counter()
        X2 = _nan_safe_center(X)

        if self.task == "auto":
            # 简单启发：y 为整数且类别数不大 -> 分类
            y_is_int = np.all(np.isclose(y, np.round(y)))
            n_unique = len(np.unique(y))
            task = "classification" if (y_is_int and n_unique <= max(20, int(0.1 * len(y)))) else "regression"
        else:
            task = self.task

        if task == "classification":
            scores = mutual_info_classif(
                X2, y, n_neighbors=self.n_neighbors, random_state=self.random_state, discrete_features="auto"
            )
        else:
            scores = mutual_info_regression(
                X2, y, n_neighbors=self.n_neighbors, random_state=self.random_state, discrete_features="auto"
            )

        dt = time.perf_counter() - t0
        names = _ensure_feature_names(feature_names, X.shape[1])
        return RelevanceResult(
            names,
            np.asarray(scores, dtype=float),
            meta={"runtime_sec": dt, "task": task, "n_neighbors": int(self.n_neighbors)},
        )


@dataclass
class DistanceCorrelationScorer(BaseRelevanceScorer):
    name: str = "distance_correlation"

    def fit_score(self, X: np.ndarray, y: np.ndarray, feature_names: Optional[Iterable[str]] = None) -> RelevanceResult:
        t0 = time.perf_counter()
        scores = _distance_correlation(X, y)
        dt = time.perf_counter() - t0
        names = _ensure_feature_names(feature_names, X.shape[1])
        return RelevanceResult(names, scores, meta={"runtime_sec": dt, "complexity": "O(n^2 * p)"})
