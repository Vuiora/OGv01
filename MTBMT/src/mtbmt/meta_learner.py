from __future__ import annotations

import json
from dataclasses import dataclass
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, DefaultDict

import numpy as np
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import RandomForestClassifier
from sklearn.impute import SimpleImputer
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder


@dataclass(frozen=True)
class MetaModelBundle:
    model: Pipeline
    feature_columns: List[str]
    target_name: str = "selected_method"


def _normalize_method_name(name: str) -> str:
    """
    经验库里不同实现/超参数可能产生不同 method_name：
    - "mutual_info(n_neighbors=3)" / "mutual_info(binned)" -> "mutual_info"

    这里做一个轻量归一化，避免训练时 label 被无意义拆分。
    """
    n = (name or "").strip()
    if not n:
        return ""
    if n.startswith("mutual_info"):
        return "mutual_info"
    if n in {"dcor", "distance_correlation"}:
        return "distance_correlation"
    if n in {"perm", "permutation_importance"}:
        return "permutation_importance"
    return n


def _flatten_features(meta_features: Dict[str, Any], trajectory_features: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    x = dict(meta_features)
    if trajectory_features:
        for k, v in trajectory_features.items():
            x[f"traj__{k}"] = v
    return x


def _safe_float(v: Any, default: float = float("nan")) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def _objective_j(ev: Dict[str, Any], w_utility: float, w_stability: float, w_cost: float) -> float:
    cv = _safe_float(ev.get("cv_score_mean"), default=float("nan"))
    stab = _safe_float(ev.get("stability_jaccard"), default=0.0)
    runtime = _safe_float(ev.get("runtime_sec"), default=0.0)
    if not np.isfinite(cv):
        return float("-inf")
    runtime = max(runtime, 0.0)
    return (float(w_utility) * cv) + (float(w_stability) * stab) - (float(w_cost) * float(np.log1p(runtime)))


def load_experience_jsonl(
    path: str | Path,
    *,
    label_target: str = "objective",
    w_utility: float = 1.0,
    w_stability: float = 0.10,
    w_cost: float = 0.15,
    aggregate_by_dataset: bool = False,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """
    读取经验库并返回训练用 (X_rows, y_labels)。

    与 `scripts/evaluate_meta_selector.py` 对齐：
    - label 不直接用 record 里的 selected_method，而是从 evaluations 里按 label_target 重新计算
    - 可选 aggregate_by_dataset：同一 dataset_id 多条记录先聚合为 1 条（显著提升 Top1 标签一致性）
    """
    rows: List[Dict[str, Any]] = []
    y: List[str] = []
    p = Path(path)
    if not p.exists():
        return rows, y

    # raw buffers for aggregation
    raw_rows: List[Dict[str, Any]] = []
    raw_meta: List[Dict[str, Any]] = []

    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            dataset_id = str(obj.get("dataset_id", "") or "")
            feats = _flatten_features(obj.get("meta_features", {}) or {}, obj.get("trajectory_features"))

            # normalize evaluations
            evaluations = obj.get("evaluations", {}) or {}
            norm_evals: Dict[str, Dict[str, Any]] = {}
            for k, ev in (evaluations.items() if isinstance(evaluations, dict) else []):
                ev = dict(ev or {})
                name0 = str(ev.get("method_name", k) or k)
                name = _normalize_method_name(name0)
                if not name:
                    continue
                ev["method_name"] = name
                norm_evals[name] = ev

            if len(norm_evals) < 2:
                continue

            times = {m: _safe_float(ev.get("runtime_sec"), default=float("nan")) for m, ev in norm_evals.items()}
            cvs = {m: _safe_float(ev.get("cv_score_mean"), default=float("nan")) for m, ev in norm_evals.items()}
            objs = {m: _objective_j(ev, w_utility, w_stability, w_cost) for m, ev in norm_evals.items()}

            raw_rows.append(feats)
            raw_meta.append({"dataset_id": dataset_id, "methods": sorted(norm_evals.keys()), "times": times, "cvs": cvs, "objs": objs})

    if not aggregate_by_dataset:
        for x, m in zip(raw_rows, raw_meta):
            cvs = m["cvs"]
            objs = m["objs"]
            if str(label_target).lower() == "cv":
                best = max(cvs.items(), key=lambda kv: _safe_float(kv[1], default=float("-inf")))[0]
            else:
                best = max(objs.items(), key=lambda kv: _safe_float(kv[1], default=float("-inf")))[0]
            rows.append(x)
            y.append(str(best))
        return rows, y

    # aggregate by dataset_id
    by_ds: DefaultDict[str, List[int]] = defaultdict(list)
    for i, m in enumerate(raw_meta):
        ds = str(m.get("dataset_id", "") or "")
        if ds:
            by_ds[ds].append(i)

    for ds, idxs in by_ds.items():
        keys = sorted({k for i in idxs for k in raw_rows[i].keys()})
        row_out: Dict[str, Any] = {}
        for k in keys:
            vals = [raw_rows[i].get(k, None) for i in idxs]
            nums: List[float] = []
            first_str: Optional[str] = None
            for v in vals:
                if isinstance(v, (int, float)) and v is not None and np.isfinite(float(v)):
                    nums.append(float(v))
                elif isinstance(v, str) and first_str is None:
                    first_str = v
            if nums:
                row_out[k] = float(np.mean(nums))
            elif first_str is not None:
                row_out[k] = first_str
            else:
                row_out[k] = None

        methods = sorted({mm for i in idxs for mm in raw_meta[i]["methods"]})
        if len(methods) < 2:
            continue
        times: Dict[str, float] = {}
        cvs: Dict[str, float] = {}
        objs: Dict[str, float] = {}
        for mm in methods:
            tvals = np.asarray([raw_meta[i]["times"].get(mm, float("nan")) for i in idxs], dtype=float)
            cvals = np.asarray([raw_meta[i]["cvs"].get(mm, float("nan")) for i in idxs], dtype=float)
            ovals = np.asarray([raw_meta[i]["objs"].get(mm, float("nan")) for i in idxs], dtype=float)
            times[mm] = float(np.nanmean(tvals))
            cvs[mm] = float(np.nanmean(cvals))
            objs[mm] = float(np.nanmean(ovals))

        if str(label_target).lower() == "cv":
            best = max(cvs.items(), key=lambda kv: _safe_float(kv[1], default=float("-inf")))[0]
        else:
            best = max(objs.items(), key=lambda kv: _safe_float(kv[1], default=float("-inf")))[0]

        rows.append(row_out)
        y.append(str(best))

    return rows, y


def train_meta_selector(
    experience_path: str | Path,
    *,
    random_state: int = 0,
    n_estimators: int = 300,
    label_target: str = "objective",
    w_utility: float = 1.0,
    w_stability: float = 0.10,
    w_cost: float = 0.15,
    aggregate_by_dataset: bool = True,
) -> MetaModelBundle:
    """
    用经验库训练“相关性量化算法选择器”：
    X = 数据集元特征 + 轨迹特征
    y = selected_method（历史最优方法）

    这里用 RandomForest 做一个鲁棒 baseline；后续可以替换成 XGBoost/LightGBM/TabPFN 等。
    """

    rows, y = load_experience_jsonl(
        experience_path,
        label_target=label_target,
        w_utility=w_utility,
        w_stability=w_stability,
        w_cost=w_cost,
        aggregate_by_dataset=aggregate_by_dataset,
    )
    if not rows:
        raise ValueError(f"经验库为空：{experience_path}")

    # 对齐列
    all_cols = sorted({k for r in rows for k in r.keys()})
    X_rows = [{c: r.get(c, None) for c in all_cols} for r in rows]
    # 用 DataFrame 承载，避免 sklearn 将 list[dict] 视为 1D object array
    import pandas as pd

    X = pd.DataFrame(X_rows, columns=all_cols)

    # 列类型划分：字符串 -> 类别；其余走 passthrough（None 会被 OneHotEncoder 忽略/或在模型中当缺失）
    cat_cols = [c for c in all_cols if any(isinstance(r.get(c, None), str) for r in rows)]
    num_cols = [c for c in all_cols if c not in cat_cols]

    # 与 `scripts/evaluate_meta_selector.py` 对齐：显式缺失值填充（RF 不接受 NaN）
    cat_pipe = Pipeline(
        steps=[
            ("impute", SimpleImputer(strategy="most_frequent")),
            ("onehot", OneHotEncoder(handle_unknown="ignore")),
        ]
    )
    num_pipe = Pipeline(steps=[("impute", SimpleImputer(strategy="median"))])

    pre = ColumnTransformer(
        transformers=[
            ("cat", cat_pipe, cat_cols),
            ("num", num_pipe, num_cols),
        ],
        remainder="drop",
    )

    clf = RandomForestClassifier(
        n_estimators=n_estimators,
        random_state=random_state,
        n_jobs=-1,
        class_weight="balanced",
    )

    model = Pipeline([("pre", pre), ("clf", clf)])
    model.fit(X, y)

    return MetaModelBundle(model=model, feature_columns=all_cols)


def predict_best_method(bundle: MetaModelBundle, meta_features: Dict[str, Any], trajectory_features: Optional[Dict[str, Any]] = None) -> str:
    x = _flatten_features(meta_features, trajectory_features)
    row = {c: x.get(c, None) for c in bundle.feature_columns}
    import pandas as pd

    X1 = pd.DataFrame([row], columns=bundle.feature_columns)
    pred = bundle.model.predict(X1)[0]
    return str(pred)


def predict_top_methods(
    bundle: MetaModelBundle,
    meta_features: Dict[str, Any],
    trajectory_features: Optional[Dict[str, Any]] = None,
    *,
    top_n: int = 3,
) -> List[Tuple[str, float]]:
    """
    返回 Top-N 推荐（含概率）。

    用途：当你希望“尽量只跑一个方法以节省时间”，但在模型不确定时可以给出备选方法。
    """
    x = _flatten_features(meta_features, trajectory_features)
    row = {c: x.get(c, None) for c in bundle.feature_columns}

    if not hasattr(bundle.model, "predict_proba"):
        # 退化：没有概率就只返回 best method
        return [(predict_best_method(bundle, meta_features, trajectory_features), 1.0)]

    import pandas as pd

    X1 = pd.DataFrame([row], columns=bundle.feature_columns)
    proba = bundle.model.predict_proba(X1)[0]
    classes = getattr(bundle.model, "classes_", None)
    if classes is None and hasattr(bundle.model, "named_steps") and "clf" in bundle.model.named_steps:
        classes = getattr(bundle.model.named_steps["clf"], "classes_", None)
    if classes is None:
        return [(predict_best_method(bundle, meta_features, trajectory_features), 1.0)]

    pairs = [(str(c), float(p)) for c, p in zip(classes, proba)]
    pairs.sort(key=lambda t: t[1], reverse=True)
    return pairs[: max(int(top_n), 1)]


def recommend_methods_for_low_runtime(
    bundle: MetaModelBundle,
    meta_features: Dict[str, Any],
    trajectory_features: Optional[Dict[str, Any]] = None,
    *,
    min_top1_prob: float = 0.60,
    min_margin: float = 0.15,
) -> List[str]:
    """
    面向“更低运行时间”的推荐策略：默认只推荐 1 个方法。

    - 若 Top1 概率足够高，或 Top1-Top2 概率差足够大 -> 只返回 Top1（最快）
    - 否则返回 [Top1, Top2]，供你在预算允许时做二次验证
    """
    top2 = predict_top_methods(bundle, meta_features, trajectory_features, top_n=2)
    if not top2:
        return [predict_best_method(bundle, meta_features, trajectory_features)]
    if len(top2) == 1:
        return [top2[0][0]]

    (m1, p1), (m2, p2) = top2[0], top2[1]
    if p1 >= float(min_top1_prob) or (p1 - p2) >= float(min_margin):
        return [m1]
    return [m1, m2]