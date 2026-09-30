"""MTBMT strategy bridge using E only, with dataset exclusion and grouped CV."""
import json
import time
from pathlib import Path
import numpy as np
from sklearn.linear_model import Ridge
from sklearn.model_selection import GroupKFold
from mtbmt.meta_features import compute_dataset_meta_features
from mtbmt.meta_learner import train_meta_selector, predict_top_methods
from mtbmt.relevance.filters import PearsonAbsScorer, SpearmanAbsScorer, MutualInfoScorer

METHODS = {"pearson_abs": PearsonAbsScorer, "spearman_abs": SpearmanAbsScorer, "mutual_info": MutualInfoScorer}

def recommend(records, features, target, group_field, top_k, history: Path, dataset_id, source_hash, scratch: Path):
    X = np.asarray([[r["values"][f] for f in features] for r in records], dtype=float)
    y = np.asarray([r["values"][target] for r in records], dtype=float)
    groups = np.asarray([str(r["group_ids"][group_field]) for r in records])
    if len(set(groups)) < 6:
        raise ValueError("E partition requires at least 6 distinct groups")
    meta = compute_dataset_meta_features(X, y).as_dict()
    historical = []
    if history.exists():
        for line in history.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            reason = record.get("selection_reason", {})
            if record.get("dataset_id") == dataset_id or reason.get("source_sha256") == source_hash:
                continue
            if record.get("selected_method") in METHODS and reason.get("evidence_purpose") == "E":
                historical.append(record)
    distinct = len({r["dataset_id"] for r in historical})
    predictions, model_error = [], None
    if distinct >= 3:
        filtered = scratch / "meta-training.jsonl"
        filtered.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in historical), encoding="utf-8")
        try:
            bundle = train_meta_selector(filtered, n_estimators=64, random_state=0,
                label_target="objective", w_utility=1.0, w_stability=0.1, w_cost=0.0,
                aggregate_by_dataset=True)
            predictions = predict_top_methods(bundle, meta, top_n=3)
        except (ValueError, TypeError):
            model_error = "Historical selector training failed; using measured E grouped-CV cold start"
    evaluations = {}
    for name, factory in METHODS.items():
        started = time.perf_counter()
        scores, sets = [], []
        for train, test in GroupKFold(n_splits=3).split(X, y, groups):
            scorer = factory()
            relevance = scorer.fit_score(X[train], y[train], feature_names=features)
            indices = np.argsort(-relevance.scores, kind="stable")[:top_k]
            sets.append(set(indices.tolist()))
            # Regression is explicit even when integer-valued measurements resemble classes.
            fitted = Ridge(alpha=1.0).fit(X[train][:, indices], y[train])
            errors = y[test] - fitted.predict(X[test][:, indices])
            scores.append(-float(np.mean(errors ** 2)))
        stability = sum(len(a & b) / len(a | b) for i, a in enumerate(sets) for b in sets[i+1:]) / 3
        evaluations[name] = {"cv_score_mean": float(np.mean(scores)), "cv_score_std": float(np.std(scores)),
            "stability_jaccard": stability, "runtime_sec": time.perf_counter() - started,
            "k": top_k, "objective": float(np.mean(scores)) + 0.1 * stability,
            "cv": "GroupKFold(3), E only", "metric": "negative_mse"}
    measured_best = max(evaluations, key=lambda name: evaluations[name]["objective"])
    predicted = [name for name, _ in predictions if name in METHODS]
    selected = predicted[0] if predicted else measured_best
    relevance = METHODS[selected]().fit_score(X, y, feature_names=features)
    ranked = np.argsort(-relevance.scores, kind="stable").tolist()
    return {"selected_method": selected, "measured_best_method": measured_best,
        "mode": "historical_meta_selector" if predicted else "cold_start_grouped_cv",
        "meta_features": meta, "evaluations": evaluations,
        "recommendations": [{"method": m, "probability": p} for m, p in predictions],
        "selected_features": [features[i] for i in ranked[:top_k]],
        "feature_scores": {features[i]: float(relevance.scores[i]) for i in ranked},
        "historical_dataset_count": distinct, "model_error": model_error,
        "evidence_purpose": "E", "source_sha256": source_hash,
        "note": "Recommendations affect exploration only; CV scores are not confirmation evidence."}

