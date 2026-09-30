from __future__ import annotations

"""
Legacy trajectory utilities.

This module exists to preserve the original "Trajectary" API used by some docs
and scripts, while keeping it under the main `mtbmt` package.

Prefer newer modules where possible:
- `mtbmt.decision_tree_trajectory`
- `mtbmt.trajectory_features`
- `mtbmt.trajectory_guidance`
"""

import json
from typing import Any, Dict, Optional

import numpy as np
from sklearn.preprocessing import LabelEncoder, StandardScaler


class Trajectary:
    """
    A lightweight trajectory container + featurizer.

    trajectory_dict convention:
    - tendency: list[float]
    - sequence: list[int]
    - selection: list[str]
    - retrieval_time: float (optional, default 0.0)
    - trajectory_length: int (optional; auto-aligned to actual length)
    - decision_effect: float (optional, default 0.0)
    """

    def __init__(self, file_name: str, root_Node_array: Optional[list] = None, trajectory_dict: Optional[dict] = None):
        self.file_name = file_name
        self.root_Node_array = root_Node_array if root_Node_array is not None else []

        if trajectory_dict is not None:
            self.trajectory_dict: Dict[str, Any] = dict(trajectory_dict)
        else:
            try:
                with open(self.file_name, "r", encoding="utf-8") as f:
                    self.trajectory_dict = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                self.trajectory_dict = {}

        self._validate_trajectory_labels()

    def _validate_trajectory_labels(self) -> None:
        required = ["retrieval_time", "trajectory_length", "decision_effect"]
        missing = [k for k in required if k not in self.trajectory_dict]

        if "trajectory_length" in missing:
            if "tendency" in self.trajectory_dict:
                self.trajectory_dict["trajectory_length"] = len(self.trajectory_dict.get("tendency", []))
            elif "sequence" in self.trajectory_dict:
                self.trajectory_dict["trajectory_length"] = len(self.trajectory_dict.get("sequence", []))
            else:
                self.trajectory_dict["trajectory_length"] = 0

        if "retrieval_time" in missing or self.trajectory_dict.get("retrieval_time") is None:
            self.trajectory_dict["retrieval_time"] = 0.0
        if "decision_effect" in missing or self.trajectory_dict.get("decision_effect") is None:
            self.trajectory_dict["decision_effect"] = 0.0
        if self.trajectory_dict.get("trajectory_length") is None:
            self.trajectory_dict["trajectory_length"] = 0

        # Align with actual length if possible
        if "tendency" in self.trajectory_dict:
            actual = len(self.trajectory_dict.get("tendency", []))
            if int(self.trajectory_dict.get("trajectory_length", 0)) != actual:
                self.trajectory_dict["trajectory_length"] = actual

    def preprocess(self, np_selection_dict: np.ndarray) -> np.ndarray:
        le = LabelEncoder()
        return le.fit_transform(np_selection_dict)

    def cal_mixed_tendency_sequence_selection(self, method: str = "concatenate", normalize: bool = False) -> np.ndarray:
        assert len(self.np_tendency_dict) == len(self.np_sequence_dict) == len(self.np_selection_dict), "三个数组长度必须一致"

        tendency = self.np_tendency_dict.astype(np.float64)
        sequence = self.np_sequence_dict.astype(np.float64)
        selection = self.np_selection_dict.astype(np.float64)

        if normalize:
            t = (tendency - tendency.min()) / (tendency.max() - tendency.min() + 1e-8)
            s = (sequence - sequence.min()) / (sequence.max() - sequence.min() + 1e-8)
            sel = (selection - selection.min()) / (selection.max() - selection.min() + 1e-8)
        else:
            t, s, sel = tendency, sequence, selection

        if method == "concatenate":
            self.mixed_feature_values = np.column_stack([t, s, sel])
        elif method == "with_interaction":
            original = np.column_stack([t, s, sel])
            inter = np.column_stack([t * s, t * sel, s * sel])
            self.mixed_feature_values = np.column_stack([original, inter])
        elif method == "with_statistics":
            original = np.column_stack([t, s, sel])
            t_mean, s_mean, sel_mean = t.mean(), s.mean(), sel.mean()
            t_std, s_std, sel_std = t.std(), s.std(), sel.std()
            centered = np.column_stack([t - t_mean, s - s_mean, sel - sel_mean])
            standardized = np.column_stack(
                [
                    (t - t_mean) / (t_std + 1e-8),
                    (s - s_mean) / (s_std + 1e-8),
                    (sel - sel_mean) / (sel_std + 1e-8),
                ]
            )
            self.mixed_feature_values = np.column_stack([original, centered, standardized])
        elif method == "full":
            original = np.column_stack([t, s, sel])
            inter = np.column_stack([t * s, t * sel, s * sel])
            t_mean, s_mean, sel_mean = t.mean(), s.mean(), sel.mean()
            centered = np.column_stack([t - t_mean, s - s_mean, sel - sel_mean])
            scaler = StandardScaler()
            standardized = scaler.fit_transform(np.column_stack([t, s, sel]))
            self.mixed_feature_values = np.column_stack([original, inter, centered, standardized])
        else:
            raise ValueError("Unknown method: %s. Choose from 'concatenate', 'with_interaction', 'with_statistics', 'full'" % method)

        return self.mixed_feature_values

    def get_trajectory_labels(self) -> Dict[str, Any]:
        return {
            "retrieval_time": float(self.trajectory_dict.get("retrieval_time", 0.0)),
            "trajectory_length": int(self.trajectory_dict.get("trajectory_length", 0)),
            "decision_effect": float(self.trajectory_dict.get("decision_effect", 0.0)),
        }

    def set_trajectory_labels(
        self,
        retrieval_time: Optional[float] = None,
        trajectory_length: Optional[int] = None,
        decision_effect: Optional[float] = None,
    ) -> None:
        if retrieval_time is not None:
            self.trajectory_dict["retrieval_time"] = float(retrieval_time)
        if trajectory_length is not None:
            self.trajectory_dict["trajectory_length"] = int(trajectory_length)
        if decision_effect is not None:
            self.trajectory_dict["decision_effect"] = float(decision_effect)
        self._validate_trajectory_labels()

    def trajectory_valulization(self, trajectory_dict: Optional[dict] = None, method: str = "concatenate", normalize: bool = False) -> np.ndarray:
        # Keep original method name (typo) for compatibility.
        if trajectory_dict is not None:
            self.trajectory_dict.update(dict(trajectory_dict))
            self._validate_trajectory_labels()

        self.np_tendency_dict = np.asarray(self.trajectory_dict.get("tendency", []), dtype=np.float64)
        self.np_sequence_dict = np.asarray(self.trajectory_dict.get("sequence", []), dtype=np.float64)
        self.np_selection_dict = self.preprocess(np.asarray(self.trajectory_dict.get("selection", []), dtype=object))
        return self.cal_mixed_tendency_sequence_selection(method=method, normalize=normalize)

    def trajectory_supervised_learning(self, trajectory_dict: Optional[dict] = None) -> Dict[str, Any]:
        if trajectory_dict is not None:
            self.trajectory_dict.update(dict(trajectory_dict))
            self._validate_trajectory_labels()
        labels = self.get_trajectory_labels()
        features = self.trajectory_valulization(method="full", normalize=False)
        return {"features": features, "labels": labels}

    def trajectory_feature_extraction(self, feature_matrix: Optional[np.ndarray] = None) -> np.ndarray:
        if feature_matrix is None:
            feature_matrix = self.trajectory_valulization(method="full")
        return feature_matrix

    def processing(self) -> Dict[str, Any]:
        mixed = self.trajectory_valulization(method="full")
        feats = self.trajectory_feature_extraction(mixed)
        labels = self.get_trajectory_labels()
        return {"features": feats, "labels": labels}

    def save_trajectory_with_labels(self, file_name: Optional[str] = None) -> None:
        if file_name is None:
            file_name = self.file_name
        self._validate_trajectory_labels()
        with open(file_name, "w", encoding="utf-8") as f:
            json.dump(self.trajectory_dict, f, ensure_ascii=False, indent=2)


__all__ = ["Trajectary"]

