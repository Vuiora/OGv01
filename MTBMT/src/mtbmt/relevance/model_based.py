from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
from sklearn.inspection import permutation_importance
from sklearn.model_selection import StratifiedKFold, KFold

from .base import BaseRelevanceScorer, RelevanceResult, _ensure_feature_names


@dataclass
class PermutationImportanceScorer(BaseRelevanceScorer):
    """
    模型无关的置换重要性（Permutation Importance）。

    关键点：
    - 分数是“置换某特征后性能下降的期望值”，越大越重要
    - 可通过重复置换得到不确定性（std）
    """

    estimator: object
    scoring: Optional[str] = None
    n_repeats: int = 5
    random_state: int = 0
    cv: int = 3
    task: str = "auto"  # "classification" | "regression" | "auto"

    @property
    def name(self) -> str:  # type: ignore[override]
        est_name = self.estimator.__class__.__name__
        return f"permutation_importance({est_name},repeats={self.n_repeats},cv={self.cv})"

    def fit_score(self, X: np.ndarray, y: np.ndarray, feature_names: Optional[Iterable[str]] = None) -> RelevanceResult:
        from sklearn.base import clone

        t0 = time.perf_counter()
        names = _ensure_feature_names(feature_names, X.shape[1])

        # 简单任务推断：整数且类别数不大 -> 分类
        if self.task == "auto":
            y_is_int = np.all(np.isclose(y, np.round(y)))
            n_unique = len(np.unique(y))
            task = "classification" if (y_is_int and n_unique <= max(20, int(0.1 * len(y)))) else "regression"
        else:
            task = self.task

        if task == "classification":
            splitter = StratifiedKFold(n_splits=self.cv, shuffle=True, random_state=self.random_state)
        else:
            splitter = KFold(n_splits=self.cv, shuffle=True, random_state=self.random_state)

        importances = []
        uncertainties = []
        for tr, te in splitter.split(X, y if task == "classification" else None):
            est = clone(self.estimator)
            est.fit(X[tr], y[tr])
            r = permutation_importance(
                est,
                X[te],
                y[te],
                scoring=self.scoring,
                n_repeats=self.n_repeats,
                random_state=self.random_state,
            )
            importances.append(r.importances_mean)
            uncertainties.append(r.importances_std)

        scores = np.mean(np.stack(importances, axis=0), axis=0)
        uncs = np.mean(np.stack(uncertainties, axis=0), axis=0)
        dt = time.perf_counter() - t0
        return RelevanceResult(names, scores, uncertainties=uncs, meta={"runtime_sec": dt, "task": task})
