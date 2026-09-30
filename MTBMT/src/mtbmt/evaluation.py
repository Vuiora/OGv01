from __future__ import annotations

import time
import warnings
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional

import numpy as np
from sklearn.base import clone
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.exceptions import ConvergenceWarning
from sklearn.metrics import get_scorer
from sklearn.model_selection import StratifiedKFold, KFold

from .meta_features import infer_task
from .relevance.base import BaseRelevanceScorer


@dataclass(frozen=True)
class MethodEvaluation:
    method_name: str
    k: int
    cv_score_mean: float
    cv_score_std: float
    stability_jaccard: float
    runtime_sec: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "method_name": self.method_name,
            "k": self.k,
            "cv_score_mean": self.cv_score_mean,
            "cv_score_std": self.cv_score_std,
            "stability_jaccard": self.stability_jaccard,
            "runtime_sec": self.runtime_sec,
        }


def _jaccard(a: Iterable[int], b: Iterable[int]) -> float:
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / max(len(sa | sb), 1)


def evaluate_relevance_method(
    X: np.ndarray,
    y: np.ndarray,
    scorer: BaseRelevanceScorer,
    *,
    feature_names: Optional[List[str]] = None,
    k: int = 20,
    cv: int = 5,
    scoring: Optional[str] = None,
    estimator: Optional[object] = None,
    random_state: int = 0,
) -> MethodEvaluation:
    """
    用“选出 top-k 特征 -> 用轻量模型做CV”的方式评估相关性量化算法。

    指标：
    - cv_score_mean/std：泛化效果（越大越好，取决于 scoring 定义）
    - stability_jaccard：不同折上 top-k 的一致性（越大越稳定）
    - runtime_sec：相关性打分 + CV 训练的总耗时
    """

    t0 = time.perf_counter()
    approx_task, y_unique, _ = infer_task(y)

    if scoring is None:
        if approx_task == "classification":
            # 二分类：roc_auc；多分类：默认用 accuracy（roc_auc 需要显式 multi_class 配置，容易因数据集而报错）
            scoring = "roc_auc" if int(y_unique) == 2 else "accuracy"
        else:
            scoring = "r2"

    if estimator is None:
        estimator = (
            LogisticRegression(max_iter=2000, n_jobs=None)
            if approx_task == "classification"
            else Ridge(alpha=1.0, random_state=random_state)
        )

    if approx_task == "classification":
        splitter = StratifiedKFold(n_splits=cv, shuffle=True, random_state=random_state)
        split_iter = splitter.split(X, y)
    else:
        splitter = KFold(n_splits=cv, shuffle=True, random_state=random_state)
        split_iter = splitter.split(X, None)

    score_fn = get_scorer(scoring)
    fold_scores: List[float] = []
    fold_topk: List[List[int]] = []

    for tr, te in split_iter:
        rel = scorer.fit_score(X[tr], y[tr], feature_names=feature_names)
        idx = np.argsort(-rel.scores)[:k].tolist()
        fold_topk.append(idx)

        est = clone(estimator)
        # 大量数据集批跑时 LogisticRegression 可能出现收敛告警；对本项目的“相对比较”影响不大，
        # 但会刷屏影响可用性，所以这里静默 ConvergenceWarning。
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=ConvergenceWarning)
            est.fit(X[tr][:, idx], y[tr])
        fold_scores.append(float(score_fn(est, X[te][:, idx], y[te])))

    # 稳定性：两两Jaccard平均
    jac = 0.0
    cnt = 0
    for i in range(len(fold_topk)):
        for j in range(i + 1, len(fold_topk)):
            jac += _jaccard(fold_topk[i], fold_topk[j])
            cnt += 1
    stability = jac / cnt if cnt else 1.0

    dt = time.perf_counter() - t0
    return MethodEvaluation(
        method_name=scorer.name,
        k=int(k),
        cv_score_mean=float(np.mean(fold_scores)),
        cv_score_std=float(np.std(fold_scores)),
        stability_jaccard=float(stability),
        runtime_sec=float(dt),
    )
