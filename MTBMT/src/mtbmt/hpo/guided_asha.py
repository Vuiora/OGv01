from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from sklearn.ensemble import RandomForestClassifier

from mtbmt.meta_features import compute_dataset_meta_features
from mtbmt.trajectory_features import compute_trajectory_features


@dataclass(frozen=True)
class HpoConfig:
    """A single hyper-parameter configuration (numeric-only for simplicity)."""

    params: Dict[str, float]


@dataclass(frozen=True)
class TrialObservation:
    """One evaluation of a config at a given resource level."""

    config: HpoConfig
    rung: int
    resource: int
    score: float  # higher is better
    runtime_sec: float
    traj_values: Tuple[float, ...]  # e.g. per-epoch val metric


@dataclass(frozen=True)
class AshaRunResult:
    observations: List[TrialObservation]
    best_score: float
    best_config: HpoConfig
    total_runtime_sec: float

    def best_at_resource(self, resource: int) -> Optional[TrialObservation]:
        obs = [o for o in self.observations if int(o.resource) == int(resource)]
        if not obs:
            return None
        return max(obs, key=lambda o: float(o.score))


def _safe_float(x: Any, default: float = float("nan")) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _normalize_01(x: np.ndarray) -> np.ndarray:
    x = np.asarray(x, dtype=float)
    if x.size == 0:
        return x
    mn = float(np.min(x))
    mx = float(np.max(x))
    if math.isclose(mx, mn):
        return np.zeros_like(x)
    return (x - mn) / (mx - mn + 1e-12)


def _default_rng(seed: int) -> np.random.Generator:
    return np.random.default_rng(int(seed))


def _sample_configs(
    *,
    rng: np.random.Generator,
    n: int,
    space: Dict[str, Tuple[float, float, str]],
) -> List[HpoConfig]:
    """
    space: name -> (low, high, prior)
      - prior="log": sample log-uniform in [low, high] (must be >0)
      - prior="uniform": sample uniform in [low, high]
    """
    out: List[HpoConfig] = []
    for _ in range(int(n)):
        params: Dict[str, float] = {}
        for k, (lo, hi, prior) in space.items():
            prior = (prior or "uniform").lower()
            if prior == "log":
                lo2 = max(float(lo), 1e-12)
                hi2 = max(float(hi), lo2 * (1.0 + 1e-12))
                v = float(np.exp(rng.uniform(np.log(lo2), np.log(hi2))))
            else:
                v = float(rng.uniform(float(lo), float(hi)))
            params[str(k)] = v
        out.append(HpoConfig(params=params))
    return out


def _build_resource_levels(*, min_resource: int, max_resource: int, eta: int) -> List[int]:
    r = max(1, int(min_resource))
    R = max(r, int(max_resource))
    eta = max(2, int(eta))
    levels = [r]
    while levels[-1] < R:
        nxt = min(R, int(levels[-1] * eta))
        if nxt == levels[-1]:
            break
        levels.append(nxt)
    return levels


ObjectiveFn = Callable[[HpoConfig, int, int], Tuple[float, Sequence[float]]]
# signature: (config, resource, seed) -> (score, trajectory_values)


def run_asha(
    *,
    objective: ObjectiveFn,
    search_space: Dict[str, Tuple[float, float, str]],
    seed: int,
    n0: int = 27,
    eta: int = 3,
    min_resource: int = 1,
    max_resource: int = 27,
) -> AshaRunResult:
    """
    Baseline ASHA: promotion is purely by current score.
    """
    rng = _default_rng(seed)
    levels = _build_resource_levels(min_resource=min_resource, max_resource=max_resource, eta=eta)

    configs = _sample_configs(rng=rng, n=n0, space=search_space)
    observations: List[TrialObservation] = []
    t0 = time.perf_counter()

    current = configs
    for rung, resource in enumerate(levels):
        rung_obs: List[TrialObservation] = []
        for cfg in current:
            t1 = time.perf_counter()
            score, traj = objective(cfg, int(resource), int(seed))
            dt = time.perf_counter() - t1
            o = TrialObservation(
                config=cfg,
                rung=int(rung),
                resource=int(resource),
                score=float(score),
                runtime_sec=float(dt),
                traj_values=tuple(float(x) for x in traj),
            )
            observations.append(o)
            rung_obs.append(o)

        # promote top 1/eta
        if rung == len(levels) - 1:
            break
        rung_obs.sort(key=lambda o: float(o.score), reverse=True)
        k = max(1, int(math.ceil(len(rung_obs) / float(eta))))
        current = [o.config for o in rung_obs[:k]]

    total_dt = time.perf_counter() - t0
    best = max(observations, key=lambda o: float(o.score))
    return AshaRunResult(
        observations=observations,
        best_score=float(best.score),
        best_config=best.config,
        total_runtime_sec=float(total_dt),
    )


def _hpo_feature_row(
    *,
    dataset_meta: Dict[str, object],
    cfg: HpoConfig,
    rung: int,
    resource: int,
    score: float,
    traj_values: Sequence[float],
) -> Dict[str, float | str | None]:
    """
    Candidate row for reranker: meta + search state + config params + trajectory features.
    Keep it flat like other modules in this repo.
    """
    row: Dict[str, float | str | None] = {}
    for k, v in (dataset_meta or {}).items():
        kk = str(k)
        if isinstance(v, bool):
            row[kk] = float(v)
        elif isinstance(v, (int, float)) and v is not None and np.isfinite(float(v)):
            row[kk] = float(v)
        elif kk == "approx_task" and isinstance(v, str):
            # keep a stable numeric encoding for the key categorical meta feature
            vv = v.strip().lower()
            row[kk] = 1.0 if vv == "classification" else 0.0
        else:
            # keep as-is (may be ignored by the minimal numeric-only reranker)
            row[kk] = v

    # search state
    row["rung"] = float(rung)
    row["resource"] = float(resource)
    row["score"] = float(score)

    # config params
    for k, v in (cfg.params or {}).items():
        row[f"hp__{k}"] = float(v)

    # trajectory features (optional)
    tf = compute_trajectory_features(traj_values, normalize=True)
    if tf is not None:
        for k, v in tf.as_dict().items():
            row[f"traj__{k}"] = v

    return row


class HpoReranker:
    """
    A simple "tendency" model for promotion decisions.

    It learns to predict whether a config will yield a large improvement when promoted
    to the next resource level (binary label).
    """

    def __init__(self, *, random_state: int = 0, n_estimators: int = 400):
        self.model = RandomForestClassifier(
            n_estimators=int(n_estimators),
            random_state=int(random_state),
            n_jobs=-1,
            class_weight="balanced",
        )
        self.columns: Optional[List[str]] = None

    def fit(self, X_rows: List[Dict[str, Any]], y: List[int]) -> "HpoReranker":
        if not X_rows:
            raise ValueError("empty training rows for HpoReranker")
        cols = sorted({k for r in X_rows for k in r.keys()})
        X = np.asarray([[r.get(c, np.nan) for c in cols] for r in X_rows], dtype=object)

        # convert to float where possible; keep strings as object -> fallback to nan
        Xf = np.full((X.shape[0], X.shape[1]), np.nan, dtype=float)
        for j in range(X.shape[1]):
            for i in range(X.shape[0]):
                v = X[i, j]
                if isinstance(v, (int, float)) and v is not None and np.isfinite(float(v)):
                    Xf[i, j] = float(v)
                else:
                    # keep NaN (we do not one-hot here; keep minimal dependencies)
                    Xf[i, j] = np.nan

        med = np.nanmedian(Xf, axis=0)
        idx = np.where(~np.isfinite(Xf))
        Xf[idx] = med[idx[1]]

        self.model.fit(Xf, np.asarray(y, dtype=int))
        self.columns = cols
        return self

    def predict_proba_1(self, rows: List[Dict[str, Any]]) -> np.ndarray:
        if self.columns is None:
            raise ValueError("HpoReranker not fitted")
        cols = self.columns
        X = np.asarray([[r.get(c, np.nan) for c in cols] for r in rows], dtype=object)
        Xf = np.full((X.shape[0], X.shape[1]), np.nan, dtype=float)
        for j in range(X.shape[1]):
            for i in range(X.shape[0]):
                v = X[i, j]
                if isinstance(v, (int, float)) and v is not None and np.isfinite(float(v)):
                    Xf[i, j] = float(v)
                else:
                    Xf[i, j] = np.nan

        med = np.nanmedian(Xf, axis=0)
        idx = np.where(~np.isfinite(Xf))
        Xf[idx] = med[idx[1]]

        proba = self.model.predict_proba(Xf)
        if proba.shape[1] == 2:
            return proba[:, 1]
        return np.ones((Xf.shape[0],), dtype=float) / float(max(1, Xf.shape[0]))


@dataclass(frozen=True)
class GuidedAshaRunResult(AshaRunResult):
    promote_disagree_rate: float  # fraction of promotions that differ from baseline-by-score


def collect_promotion_training_data(
    *,
    objective: ObjectiveFn,
    search_space: Dict[str, Tuple[float, float, str]],
    X: np.ndarray,
    y: np.ndarray,
    seed: int,
    n0: int,
    eta: int,
    min_resource: int,
    max_resource: int,
    positive_top_frac: float = 0.35,
    label_kind: str = "uplift",  # uplift|next_score
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """
    Run baseline ASHA once and build a supervised dataset:
    - Each promoted config produces a training row at its current rung
    - Label is 1 if the chosen label signal is in the top fraction among promoted configs of that rung.
      label_kind:
        - "uplift":     o2.score - o1.score (current default, can be noisy if curves are flat)
        - "next_score": o2.score (often less noisy than uplift; focuses on immediate next rung performance)

    This is intentionally lightweight: it only uses promotions we actually evaluate.
    """
    meta = compute_dataset_meta_features(X, y).as_dict()
    levels = _build_resource_levels(min_resource=min_resource, max_resource=max_resource, eta=eta)

    rng = _default_rng(seed)
    configs = _sample_configs(rng=rng, n=n0, space=search_space)

    # store obs by (rung, config_id)
    def _cfg_id(c: HpoConfig) -> str:
        items = sorted(c.params.items())
        return "|".join(f"{k}={v:.8g}" for k, v in items)

    obs_by_rung_cfg: Dict[Tuple[int, str], TrialObservation] = {}
    current = configs
    for rung, resource in enumerate(levels):
        rung_obs: List[TrialObservation] = []
        for cfg in current:
            t1 = time.perf_counter()
            score, traj = objective(cfg, int(resource), int(seed))
            dt = time.perf_counter() - t1
            o = TrialObservation(
                config=cfg,
                rung=int(rung),
                resource=int(resource),
                score=float(score),
                runtime_sec=float(dt),
                traj_values=tuple(float(x) for x in traj),
            )
            obs_by_rung_cfg[(int(rung), _cfg_id(cfg))] = o
            rung_obs.append(o)

        if rung == len(levels) - 1:
            break
        rung_obs.sort(key=lambda o: float(o.score), reverse=True)
        k = max(1, int(math.ceil(len(rung_obs) / float(eta))))
        promoted = rung_obs[:k]
        current = [o.config for o in promoted]

    # Build training rows from promotions where we have next rung score
    X_rows: List[Dict[str, Any]] = []
    y_rows: List[int] = []
    lk = (label_kind or "uplift").strip().lower()
    if lk not in {"uplift", "next_score"}:
        raise ValueError(f"unknown label_kind={label_kind!r}, expected 'uplift' or 'next_score'")
    for rung in range(len(levels) - 1):
        resource = levels[rung]
        next_resource = levels[rung + 1]
        # promoted configs are those that appear at next rung
        promoted_cfg_ids = [cfg_id for (rr, cfg_id) in obs_by_rung_cfg.keys() if rr == rung + 1]
        pairs: List[Tuple[TrialObservation, TrialObservation, float]] = []
        for cfg_id in promoted_cfg_ids:
            o1 = obs_by_rung_cfg.get((rung, cfg_id))
            o2 = obs_by_rung_cfg.get((rung + 1, cfg_id))
            if o1 is None or o2 is None:
                continue
            uplift = float(o2.score - o1.score)
            signal = float(o2.score) if lk == "next_score" else float(uplift)
            pairs.append((o1, o2, signal))
        if not pairs:
            continue

        vals = np.asarray([u for _a, _b, u in pairs], dtype=float)
        # positive if signal is in top fraction
        q = float(np.quantile(vals, 1.0 - float(positive_top_frac)))
        for o1, _o2, v in pairs:
            row = _hpo_feature_row(
                dataset_meta=meta,
                cfg=o1.config,
                rung=int(rung),
                resource=int(resource),
                score=float(o1.score),
                traj_values=o1.traj_values,
            )
            X_rows.append(row)
            y_rows.append(1 if float(v) >= q else 0)

    return X_rows, y_rows


def run_guided_asha(
    *,
    objective: ObjectiveFn,
    search_space: Dict[str, Tuple[float, float, str]],
    X: np.ndarray,
    y: np.ndarray,
    seed: int,
    reranker: HpoReranker,
    n0: int = 27,
    eta: int = 3,
    min_resource: int = 1,
    max_resource: int = 27,
    mode: str = "rerank",
    shortlist_k: int = 9,
    alpha: float = 0.6,
    max_swaps: Optional[int] = None,
    min_mix_margin: float = 0.0,
    min_proba: float = 0.0,
) -> GuidedAshaRunResult:
    """
    Guided-ASHA:
    - At each rung, baseline ranks by current score.
    - We then rerank candidates for promotion using:
        mix = alpha * p(uplift_high) + (1-alpha) * score_norm
    - mode:
        - "rerank": only rerank the top shortlist_k by score (safer)
        - "replace": rerank all candidates (more aggressive)

    Noise-reduction guardrails (optional):
    - max_swaps: maximum number of replacements against baseline promotions per rung.
      Setting a small value makes guidance more conservative.
    - min_mix_margin: only swap-in if the candidate beats the current worst promoted by at least this margin in mix score.
    - min_proba: only allow swap-in if the candidate's reranker probability >= this threshold.
      Useful when RF probabilities are noisy around 0.5.
    """
    meta = compute_dataset_meta_features(X, y).as_dict()
    rng = _default_rng(seed)
    levels = _build_resource_levels(min_resource=min_resource, max_resource=max_resource, eta=eta)

    configs = _sample_configs(rng=rng, n=n0, space=search_space)
    observations: List[TrialObservation] = []
    t0 = time.perf_counter()

    promote_total = 0
    promote_disagree = 0

    current = configs
    for rung, resource in enumerate(levels):
        rung_obs: List[TrialObservation] = []
        for cfg in current:
            t1 = time.perf_counter()
            score, traj = objective(cfg, int(resource), int(seed))
            dt = time.perf_counter() - t1
            o = TrialObservation(
                config=cfg,
                rung=int(rung),
                resource=int(resource),
                score=float(score),
                runtime_sec=float(dt),
                traj_values=tuple(float(x) for x in traj),
            )
            observations.append(o)
            rung_obs.append(o)

        if rung == len(levels) - 1:
            break

        # baseline promotions by score
        rung_obs.sort(key=lambda o: float(o.score), reverse=True)
        k = max(1, int(math.ceil(len(rung_obs) / float(eta))))
        baseline_promoted = [o.config for o in rung_obs[:k]]

        # guided promotions
        if mode.strip().lower() == "rerank":
            cand_pool = rung_obs[: max(1, min(int(shortlist_k), len(rung_obs)))]
        else:
            cand_pool = rung_obs

        rows = [
            _hpo_feature_row(
                dataset_meta=meta,
                cfg=o.config,
                rung=int(rung),
                resource=int(resource),
                score=float(o.score),
                traj_values=o.traj_values,
            )
            for o in cand_pool
        ]
        proba = reranker.predict_proba_1(rows)
        score_norm = _normalize_01(np.asarray([o.score for o in cand_pool], dtype=float))
        mix = float(alpha) * np.asarray(proba, dtype=float) + (1.0 - float(alpha)) * score_norm

        # Start from baseline promotions, then do conservative swap-in from candidate pool.
        # When max_swaps=k and min_mix_margin=0, this converges to "top-k by mix" within cand_pool (matches previous behavior).
        max_swaps_eff = k if max_swaps is None else max(0, int(max_swaps))
        min_mix_margin_eff = float(min_mix_margin)
        min_proba_eff = float(min_proba)

        # Build quick lookup from config identity to position in cand_pool.
        idx_by_cfg = {id(o.config): i for i, o in enumerate(cand_pool)}
        baseline_idx = [idx_by_cfg.get(id(c), None) for c in baseline_promoted]
        baseline_idx = [i for i in baseline_idx if i is not None]

        in_baseline = {int(i) for i in baseline_idx}
        extra_idx = [i for i in range(len(cand_pool)) if i not in in_baseline]

        # Sort candidates: extras by descending mix; baseline by ascending mix.
        extra_idx.sort(key=lambda i: float(mix[i]), reverse=True)
        baseline_idx.sort(key=lambda i: float(mix[i]))

        chosen_idx = list(baseline_idx)
        chosen_set = set(chosen_idx)

        swaps_done = 0
        bi = 0  # pointer to current worst baseline (ascending)
        ei = 0  # pointer to current best extra (descending)

        while swaps_done < max_swaps_eff and bi < len(baseline_idx) and ei < len(extra_idx):
            worst_i = int(baseline_idx[bi])
            best_i = int(extra_idx[ei])

            # Skip if best_i already chosen (shouldn't happen, but safe)
            if best_i in chosen_set:
                ei += 1
                continue

            # Probability guardrail
            if min_proba_eff > 0.0 and float(proba[best_i]) < min_proba_eff:
                ei += 1
                continue

            # Margin guardrail
            if float(mix[best_i]) <= float(mix[worst_i]) + min_mix_margin_eff:
                # Since extras are sorted desc, no future extra can beat this worst by margin.
                break

            # Perform swap
            chosen_set.remove(worst_i)
            chosen_set.add(best_i)
            swaps_done += 1
            bi += 1
            ei += 1

        # Choose final top-k by mix among chosen_set (stable within the selected set)
        chosen_idx = sorted(chosen_set, key=lambda i: float(mix[int(i)]), reverse=True)[:k]
        guided_promoted = [cand_pool[int(i)].config for i in chosen_idx]

        # disagreement stats (set-based)
        promote_total += 1
        bset = {id(c) for c in baseline_promoted}
        gset = {id(c) for c in guided_promoted}
        if bset != gset:
            promote_disagree += 1

        current = guided_promoted

    total_dt = time.perf_counter() - t0
    best = max(observations, key=lambda o: float(o.score))
    disagree_rate = float(promote_disagree / max(1, promote_total))
    return GuidedAshaRunResult(
        observations=observations,
        best_score=float(best.score),
        best_config=best.config,
        total_runtime_sec=float(total_dt),
        promote_disagree_rate=disagree_rate,
    )

