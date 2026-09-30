from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import numpy as np


@dataclass(frozen=True)
class RelevanceResult:
    """
    统一的特征相关性/重要性量化输出。

    - scores: 每个特征一个分数（越大越相关/越重要）
    - uncertainties: 每个特征一个不确定性（可选；越大代表越不稳定）
    - meta: 方法侧信息（如参数、耗时等）
    """

    feature_names: List[str]
    scores: np.ndarray
    uncertainties: Optional[np.ndarray] = None
    meta: Optional[Dict[str, object]] = None

    def topk(self, k: int) -> List[str]:
        k = int(k)
        if k <= 0:
            return []
        idx = np.argsort(-self.scores)[:k]
        return [self.feature_names[i] for i in idx]


class BaseRelevanceScorer:
    """
    统一接口：fit_score(X, y, feature_names) -> RelevanceResult

    约定：
    - X 为二维数组 (n_samples, n_features)
    - y 为一维数组 (n_samples,)
    - 分数越大越“相关/重要”
    """

    name: str = "base"

    def fit_score(
        self,
        X: np.ndarray,
        y: np.ndarray,
        feature_names: Optional[Iterable[str]] = None,
    ) -> RelevanceResult:
        raise NotImplementedError


def _ensure_feature_names(feature_names: Optional[Iterable[str]], n_features: int) -> List[str]:
    if feature_names is None:
        return [f"x{i}" for i in range(n_features)]
    names = list(feature_names)
    if len(names) != n_features:
        raise ValueError(f"feature_names长度({len(names)})与X列数({n_features})不一致")
    return names
