import argparse
import hashlib
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor

from mtbmt.evaluation import evaluate_relevance_method
from mtbmt.experience_store import ExperienceRecord, ExperienceStore, now_utc_iso
from mtbmt.meta_features import compute_dataset_meta_features, infer_task
from mtbmt.relevance.filters import PearsonAbsScorer, SpearmanAbsScorer, MutualInfoScorer, DistanceCorrelationScorer
from mtbmt.relevance.model_based import PermutationImportanceScorer


def _dataset_id_from_path(path: str) -> str:
    p = Path(path)
    h = hashlib.sha1(str(p.resolve()).encode("utf-8")).hexdigest()[:10]
    return f"{p.stem}-{h}"


def _parse_methods(s: str | None) -> set[str] | None:
    """
    Parse comma-separated method keys. Supported:
    - pearson
    - spearman
    - mi
    - dcor
    - perm
    """
    if s is None:
        return None
    items = [x.strip().lower() for x in s.split(",") if x.strip()]
    return set(items)


@dataclass
class _Progress:
    enabled: bool = True
    last_line_len: int = 0

    def update(self, msg: str) -> None:
        if not self.enabled:
            return
        # 单行刷新：避免引入 tqdm 依赖，兼容 PowerShell/cmd
        line = msg.replace("\n", " ")
        pad = max(self.last_line_len - len(line), 0)
        print("\r" + line + (" " * pad), end="", flush=True)
        self.last_line_len = len(line)

    def done(self) -> None:
        if not self.enabled:
            return
        print()  # 换行收尾
        self.last_line_len = 0


def main():
    ap = argparse.ArgumentParser(description="Benchmark feature relevance scorers and store experience (JSONL).")
    ap.add_argument("--csv", required=True, help="CSV dataset path")
    ap.add_argument("--target", required=True, help="Target column name")
    ap.add_argument("--k", type=int, default=20, help="Top-k features used for evaluation")
    ap.add_argument("--cv", type=int, default=5, help="CV folds")
    ap.add_argument("--store", default="experience/experience.jsonl", help="Experience store JSONL path")
    ap.add_argument("--scoring", default=None, help="sklearn scoring name (e.g., roc_auc, accuracy, r2, neg_rmse)")
    ap.add_argument(
        "--methods",
        default=None,
        help="Comma-separated subset of methods: pearson,spearman,mi,dcor,perm (default: all).",
    )
    ap.add_argument("--rf-estimators", type=int, default=300, help="RandomForest n_estimators used by permutation importance.")
    ap.add_argument("--perm-cv", type=int, default=3, help="Permutation importance CV folds inside scorer.")
    ap.add_argument("--perm-repeats", type=int, default=5, help="Permutation repeats (higher -> much slower).")
    ap.add_argument("--runs", type=int, default=1, help="How many benchmark runs to execute (append experience each run).")
    ap.add_argument(
        "--time-budget-sec",
        type=int,
        default=0,
        help="If >0, keep running benchmarks until the wall-clock budget is reached (overrides --runs).",
    )
    ap.add_argument(
        "--w-utility",
        type=float,
        default=1.0,
        help="Weight for utility (cv_score_mean) in objective J(A).",
    )
    ap.add_argument(
        "--w-stability",
        type=float,
        default=0.10,
        help="Weight for stability (stability_jaccard) in objective J(A).",
    )
    ap.add_argument(
        "--w-cost",
        type=float,
        default=0.15,
        help="Weight for cost penalty log(1+runtime_sec) in objective J(A). Higher -> prefer faster methods.",
    )
    ap.add_argument("--no-progress", action="store_true", help="Disable progress output.")
    args = ap.parse_args()

    df = pd.read_csv(args.csv)
    if args.target not in df.columns:
        raise SystemExit(f"target列不存在：{args.target}")

    y = df[args.target].to_numpy()
    Xdf = df.drop(columns=[args.target])

    # 目前默认：全转为数值；非数值列会变成 NaN（建议上游做更严谨的编码）
    X = Xdf.apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    feature_names = list(Xdf.columns)

    approx_task, _, _ = infer_task(y)
    if approx_task == "classification":
        base_est = RandomForestClassifier(n_estimators=args.rf_estimators, random_state=0, n_jobs=-1)
    else:
        base_est = RandomForestRegressor(n_estimators=args.rf_estimators, random_state=0, n_jobs=-1)

    selected = _parse_methods(args.methods)
    scorers = []
    if selected is None or "pearson" in selected:
        scorers.append(PearsonAbsScorer())
    if selected is None or "spearman" in selected:
        scorers.append(SpearmanAbsScorer())
    if selected is None or "mi" in selected:
        scorers.append(MutualInfoScorer(task=approx_task, n_neighbors=3, random_state=0))
    if selected is None or "dcor" in selected:
        scorers.append(DistanceCorrelationScorer())
    if selected is None or "perm" in selected:
        scorers.append(
            PermutationImportanceScorer(
                estimator=base_est,
                cv=args.perm_cv,
                n_repeats=args.perm_repeats,
                random_state=0,
                task=approx_task,
            )
        )

    meta = compute_dataset_meta_features(X, y).as_dict()
    store = ExperienceStore(args.store)
    progress = _Progress(enabled=not args.no_progress)

    def _objective_j(cv_score_mean: float, stability_jaccard: float, runtime_sec: float) -> float:
        # J(A)=w_u*Utility + w_s*Stability - w_c*log(1+Cost)
        return float(args.w_utility) * float(cv_score_mean) + float(args.w_stability) * float(stability_jaccard) - float(args.w_cost) * float(
            np.log1p(max(float(runtime_sec), 0.0))
        )

    def _one_run(run_idx: int) -> None:
        evaluations = {}
        eval_objs = []
        total_methods = max(len(scorers), 1)
        t_run0 = time.time()
        for mi, s in enumerate(scorers, start=1):
            progress.update(
                f"[run {run_idx}] method {mi}/{total_methods}: {s.name} "
                f"(k={args.k}, cv={args.cv}) elapsed={time.time() - t_run0:.1f}s"
            )
            ev = evaluate_relevance_method(
                X,
                y,
                s,
                feature_names=feature_names,
                k=args.k,
                cv=args.cv,
                scoring=args.scoring,
                random_state=0,
            )
            ev_dict = ev.as_dict()
            ev_dict["objective_j"] = _objective_j(ev.cv_score_mean, ev.stability_jaccard, ev.runtime_sec)
            evaluations[ev.method_name] = ev_dict
            eval_objs.append(ev)
        progress.done()

        # 选择规则：最大化综合目标 J(A)，并保留可解释的 tie-break
        eval_objs.sort(
            key=lambda e: (
                _objective_j(e.cv_score_mean, e.stability_jaccard, e.runtime_sec),
                e.cv_score_mean,
                e.stability_jaccard,
                -e.runtime_sec,
            ),
            reverse=True,
        )
        best = eval_objs[0]
        best_j = _objective_j(best.cv_score_mean, best.stability_jaccard, best.runtime_sec)

        rec = ExperienceRecord(
            dataset_id=_dataset_id_from_path(args.csv),
            meta_features=meta,
            trajectory_features=None,  # 轨迹特征需从训练过程日志导入；这里先留空
            evaluations=evaluations,
            selected_method=best.method_name,
            selection_reason={
                "rule": "maximize J(A)=w_u*cv + w_s*stability - w_c*log(1+runtime)",
                "weights": {"w_utility": args.w_utility, "w_stability": args.w_stability, "w_cost": args.w_cost},
                "best_objective_j": best_j,
                "best_metrics": evaluations[best.method_name],
                "run_idx": run_idx,
                "methods": [s.name for s in scorers],
                "params": {
                    "k": args.k,
                    "cv": args.cv,
                    "scoring": args.scoring,
                    "rf_estimators": args.rf_estimators,
                    "perm_cv": args.perm_cv,
                    "perm_repeats": args.perm_repeats,
                },
            },
            created_at_utc=now_utc_iso(),
        )
        store.append(rec)

        print("selected_method:", best.method_name)
        print("best_objective_j:", best_j)
        print("best_metrics:", evaluations[best.method_name])
        print("experience_appended_to:", str(Path(args.store).resolve()))

    if args.time_budget_sec and args.time_budget_sec > 0:
        deadline = time.time() + float(args.time_budget_sec)
        run_idx = 1
        while time.time() < deadline:
            remaining = max(deadline - time.time(), 0.0)
            progress.update(f"starting run {run_idx} (time remaining ~{remaining/60.0:.1f} min)")
            _one_run(run_idx)
            run_idx += 1
        progress.done()
    else:
        for i in range(1, int(args.runs) + 1):
            _one_run(i)


if __name__ == "__main__":
    main()
