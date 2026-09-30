from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import numpy as np


@dataclass(frozen=True)
class DatasetMetaFeatures:
    """
    数据集元特征（用于元学习算法选择）。

    目标：尽量低成本、对任务泛化、与“相关性算法适配性”强相关。
    """

    n_samples: int
    n_features: int
    p_over_n: float
    missing_rate: float
    sparsity_rate: float  # 近零比例（|x|<=eps）
    y_unique: int
    y_is_integer: bool
    approx_task: str  # "classification" | "regression"
    mean_abs_pearson_between_features: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "n_samples": self.n_samples,
            "n_features": self.n_features,
            "p_over_n": self.p_over_n,
            "missing_rate": self.missing_rate,
            "sparsity_rate": self.sparsity_rate,
            "y_unique": self.y_unique,
            "y_is_integer": self.y_is_integer,
            "approx_task": self.approx_task,
            "mean_abs_pearson_between_features": self.mean_abs_pearson_between_features,
        }


def infer_task(y: np.ndarray) -> Tuple[str, int, bool]:
    """
    粗略推断任务类型（分类/回归）。

    约束：该项目常从 CSV 读取，y 可能是字符串/类别；此时应优先按分类处理，
    否则后续默认 scoring/estimator 可能不兼容（如 roc_auc 需要分类标签）。
    """
    y = np.asarray(y)
    y_unique = int(len(np.unique(y)))

    # 非数值标签：直接视为分类任务
    if not np.issubdtype(y.dtype, np.number):
        return "classification", y_unique, False

    y_is_int = bool(np.all(np.isclose(y, np.round(y))))
    approx_task = "classification" if (y_is_int and y_unique <= max(20, int(0.1 * len(y)))) else "regression"
    return approx_task, y_unique, y_is_int


def compute_dataset_meta_features(X: np.ndarray, y: np.ndarray, *, zero_eps: float = 1e-12) -> DatasetMetaFeatures:
    X = np.asarray(X)
    y = np.asarray(y)

    n, p = X.shape
    missing_rate = float(np.isnan(X).mean()) if np.issubdtype(X.dtype, np.floating) else 0.0
    sparsity_rate = float((np.abs(np.nan_to_num(X, nan=0.0)) <= zero_eps).mean())

    approx_task, y_unique, y_is_int = infer_task(y)

    # 特征间平均|Pearson|（用少量子采样避免过重）
    rng = np.random.default_rng(0)
    col_idx = np.arange(p)
    if p > 200:
        col_idx = rng.choice(p, size=200, replace=False)
    Xsub = X[:, col_idx]
    # 缺失按列均值填充
    if np.issubdtype(Xsub.dtype, np.floating) and np.isnan(Xsub).any():
        col_mean = np.nanmean(Xsub, axis=0)
        inds = np.where(np.isnan(Xsub))
        Xsub = Xsub.copy()
        Xsub[inds] = np.take(col_mean, inds[1])
    Xc = Xsub - Xsub.mean(axis=0, keepdims=True)
    std = Xc.std(axis=0, keepdims=True) + 1e-12
    Z = Xc / std
    C = (Z.T @ Z) / max(n - 1, 1)
    # 去掉对角线
    absC = np.abs(C)
    mean_abs = float((absC.sum() - np.trace(absC)) / max(absC.size - len(absC), 1))

    return DatasetMetaFeatures(
        n_samples=int(n),
        n_features=int(p),
        p_over_n=float(p / max(n, 1)),
        missing_rate=missing_rate,
        sparsity_rate=sparsity_rate,
        y_unique=y_unique,
        y_is_integer=y_is_int,
        approx_task=approx_task,
        mean_abs_pearson_between_features=mean_abs,
    )
