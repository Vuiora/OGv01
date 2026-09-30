from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import numpy as np


@dataclass(frozen=True)
class TrajectoryFeatures:
    """
    Learning-trajectory features (for meta-learning).

    The input is typically a sequence over iterations, e.g.:
    - train/val loss
    - train/val score (AUC/Acc/RMSE, etc.)
    - gradient norms / update norms
    - aggregated stats of per-epoch feature importance vectors
    """

    length: int
    start: float
    end: float
    min_value: float
    min_index: int
    auc: float
    early_slope: float
    late_slope: float
    diff_std: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "length": self.length,
            "start": self.start,
            "end": self.end,
            "min_value": self.min_value,
            "min_index": self.min_index,
            "auc": self.auc,
            "early_slope": self.early_slope,
            "late_slope": self.late_slope,
            "diff_std": self.diff_std,
        }


def compute_trajectory_features(values: Iterable[float], *, normalize: bool = False) -> Optional[TrajectoryFeatures]:
    v = np.asarray(list(values), dtype=float)
    if v.size == 0:
        return None
    if v.size == 1:
        return TrajectoryFeatures(
            length=1,
            start=float(v[0]),
            end=float(v[0]),
            min_value=float(v[0]),
            min_index=0,
            auc=float(v[0]),
            early_slope=0.0,
            late_slope=0.0,
            diff_std=0.0,
        )

    if normalize:
        denom = np.std(v) + 1e-12
        v = (v - np.mean(v)) / denom

    length = int(v.size)
    start = float(v[0])
    end = float(v[-1])
    min_index = int(np.argmin(v))
    min_value = float(v[min_index])
    auc = float(np.trapz(v, dx=1.0))

    m = max(2, int(0.2 * length))
    # Early/late slopes: simple linear fit.
    x1 = np.arange(m, dtype=float)
    y1 = v[:m]
    early_slope = float(np.polyfit(x1, y1, deg=1)[0])

    x2 = np.arange(m, dtype=float)
    y2 = v[-m:]
    late_slope = float(np.polyfit(x2, y2, deg=1)[0])

    dv = np.diff(v)
    diff_std = float(np.std(dv))

    return TrajectoryFeatures(
        length=length,
        start=start,
        end=end,
        min_value=min_value,
        min_index=min_index,
        auc=auc,
        early_slope=early_slope,
        late_slope=late_slope,
        diff_std=diff_std,
    )


__all__ = ["TrajectoryFeatures", "compute_trajectory_features"]

