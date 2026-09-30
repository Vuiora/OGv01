from __future__ import annotations

import sys
import argparse
import json
import math
import time
import random
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import numpy as np
import pandas as pd
from sklearn.datasets import fetch_openml

# Allow running this script without installing the package (i.e. without `pip install -e .`).
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mtbmt.evaluation import evaluate_relevance_method
from mtbmt.experience_store import ExperienceRecord, ExperienceStore, now_utc_iso
from mtbmt.meta_features import compute_dataset_meta_features, infer_task
from mtbmt.relevance.filters import DistanceCorrelationScorer, MutualInfoScorer, PearsonAbsScorer, SpearmanAbsScorer
from mtbmt.relevance.model_based import PermutationImportanceScorer
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor


@dataclass
class _Progress:
    enabled: bool = True
    last_line_len: int = 0

    def update(self, msg: str) -> None:
        if not self.enabled:
            return
        line = msg.replace("\n", " ")
        pad = max(self.last_line_len - len(line), 0)
        print("\r" + line + (" " * pad), end="", flush=True)
        self.last_line_len = len(line)

    def done(self) -> None:
        if not self.enabled:
            return
        print()
        self.last_line_len = 0


def _http_get_json(url: str, *, timeout: int = 30) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"User-Agent": "mtbmt/0.1 (fetch_openml_100_and_ingest)"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode("utf-8"))


def _sleep_backoff(attempt: int, *, base_sec: float = 1.0, cap_sec: float = 30.0) -> None:
    # attempt 从 1 开始
    t = min(float(cap_sec), float(base_sec) * (2.0 ** max(int(attempt) - 1, 0)))
    # 加一点 jitter，避免瞬时拥塞时所有请求同相位重试
    t = t * (0.7 + 0.6 * random.random())
    time.sleep(t)


def _retry(fn, *, retries: int, progress: Optional[_Progress] = None, ctx: str = ""):
    """
    轻量重试器：用于网络不稳/偶发 OpenML 抖动。
    - retries=0 表示不重试（仅执行一次）
    """
    last_err: Optional[Exception] = None
    attempts = max(int(retries), 0) + 1
    for i in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_err = e
            if i >= attempts:
                break
            if progress is not None:
                progress.update(f"{ctx} retry {i}/{attempts-1} after error: {type(e).__name__}: {e}")
            _sleep_backoff(i)
    raise last_err  # type: ignore[misc]


def _iter_openml_list(*, limit: int, offset: int) -> List[Dict[str, Any]]:
    """
    从 OpenML 拉取 data/list。

    OpenML 的 API 在不同文档/镜像里存在两种常见路径形式，这里做兼容：
    - /api/v1/json/data/list?limit=...&offset=...
    - /api/v1/json/data/list/limit/{limit}/offset/{offset}
    """
    base = "https://www.openml.org"
    q = urllib.parse.urlencode({"limit": str(limit), "offset": str(offset)})
    urls = [
        f"{base}/api/v1/json/data/list?{q}",
        f"{base}/api/v1/json/data/list/limit/{int(limit)}/offset/{int(offset)}",
    ]
    last_err: Optional[Exception] = None
    for url in urls:
        try:
            obj = _http_get_json(url)
            data = obj.get("data", {}) or {}
            datasets = data.get("dataset", []) or []
            if isinstance(datasets, dict):
                datasets = [datasets]
            return list(datasets)
        except Exception as e:  # noqa: BLE001 - tool script; we want robust fallbacks
            last_err = e
            continue
    raise RuntimeError(f"无法访问 OpenML data/list（已尝试 {len(urls)} 种 URL 形式）。最后错误：{last_err}")


def _pick_candidate_ids(
    *,
    n: int,
    pool_multiplier: int,
    seed: int,
    max_instances: int,
    max_features: int,
    min_instances: int,
    min_features: int,
) -> List[Tuple[int, str]]:
    """
    返回 [(data_id, name)]，用于后续 fetch_openml。
    """
    rng = np.random.default_rng(int(seed))

    # 分页拉列表，先尽量多拿一些候选，再做过滤+抽样
    candidates: List[Tuple[int, str]] = []
    seen: set[int] = set()

    offset = 0
    page = 1000
    t0 = time.time()
    pool_n = max(int(n) * max(int(pool_multiplier), 1), int(n))
    while len(candidates) < (pool_n * 2) and offset < 20000:  # 给一个上限，避免无限抓
        rows = _iter_openml_list(limit=page, offset=offset)
        offset += page
        if not rows:
            break

        for r in rows:
            try:
                did = int(r.get("did") or r.get("id") or 0)
            except Exception:
                continue
            if did <= 0 or did in seen:
                continue
            seen.add(did)

            name = str(r.get("name") or f"openml_{did}")
            # 过滤规模（避免跑 100 个时耗时/内存爆炸）
            # OpenML list 把规模信息放在 quality: [{name,value}, ...]
            q = r.get("quality") or []
            qmap: Dict[str, str] = {}
            if isinstance(q, list):
                for it in q:
                    if not isinstance(it, dict):
                        continue
                    k = str(it.get("name") or "")
                    v = str(it.get("value") or "")
                    if k:
                        qmap[k] = v
            try:
                n_inst = int(float(qmap.get("NumberOfInstances", "0") or 0))
                n_feat = int(float(qmap.get("NumberOfFeatures", "0") or 0))
            except Exception:
                n_inst, n_feat = 0, 0
            if n_inst and (n_inst < int(min_instances) or n_inst > int(max_instances)):
                continue
            if n_feat and (n_feat < int(min_features) or n_feat > int(max_features)):
                continue

            # OpenML list 里不一定给出格式，先只按 target+规模过滤即可
            candidates.append((did, name))

        # 避免在网络慢时无反馈
        if time.time() - t0 > 10 and len(candidates) >= n:
            break

    if not candidates:
        raise RuntimeError("No candidates found from OpenML list. Check network or relax filters.")

    # 打乱并取前 pool_n（实际能入库多少取决于后续 fetch_openml 是否成功）
    idx = np.arange(len(candidates))
    rng.shuffle(idx)
    picked = [candidates[i] for i in idx[: int(pool_n)]]
    return picked


def _encode_tabular(df: pd.DataFrame) -> pd.DataFrame:
    """
    将表格特征尽量转换成 float：
    - 数值列：直接 to_numeric
    - 类别/字符串：factorize 编码成整数（缺失 -> -1）
    """
    out = {}
    for c in df.columns:
        s = df[c]
        if pd.api.types.is_numeric_dtype(s):
            out[c] = pd.to_numeric(s, errors="coerce")
        else:
            codes, _uniques = pd.factorize(s.astype("object"), sort=False)
            out[c] = pd.Series(codes, index=s.index, dtype="int64").astype("float64")
    return pd.DataFrame(out)


def _encode_target(y: pd.Series) -> Tuple[np.ndarray, Dict[str, Any]]:
    """
    返回 (y_array, meta)。
    若 y 非数值，factorize 成 int，并记录映射。
    """
    if pd.api.types.is_numeric_dtype(y):
        ya = pd.to_numeric(y, errors="coerce").to_numpy()
        return ya, {"target_encoding": "numeric"}
    codes, uniques = pd.factorize(y.astype("object"), sort=False)
    meta = {"target_encoding": "factorize", "target_classes": [str(u) for u in list(uniques)]}
    return codes.astype("int64"), meta


def _objective_j(ev: Dict[str, Any], w_utility: float, w_stability: float, w_cost: float) -> float:
    cv = float(ev.get("cv_score_mean", float("nan")))
    stab = float(ev.get("stability_jaccard", 0.0))
    runtime = float(ev.get("runtime_sec", 0.0))
    if not math.isfinite(cv):
        return float("-inf")
    runtime = max(runtime, 0.0)
    return (float(w_utility) * cv) + (float(w_stability) * stab) - (float(w_cost) * math.log1p(runtime))


def _already_ingested_dataset_ids(path: Path) -> set[str]:
    if not path.exists():
        return set()
    out: set[str] = set()
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            did = str(obj.get("dataset_id", "") or "")
            if did:
                out.add(did)
    return out


def _append_jsonl(path: Path, obj: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def _parse_methods(s: str | None) -> set[str] | None:
    """
    支持的 method key（与 benchmark_relevance.py 对齐）：
    - pearson
    - spearman
    - mi
    - dcor
    - perm
    """
    if s is None:
        return None
    items = [x.strip().lower() for x in str(s).split(",") if x.strip()]
    return set(items)


def _build_scorers(
    *,
    selected: Optional[set[str]],
    approx_task: str,
    base_est: object,
    n_samples: int,
    perm_cv: int,
    perm_repeats: int,
    dcor_max_n: int,
) -> List[object]:
    scorers: List[object] = []
    if selected is None or "pearson" in selected:
        scorers.append(PearsonAbsScorer())
    if selected is None or "spearman" in selected:
        scorers.append(SpearmanAbsScorer())
    if selected is None or "mi" in selected:
        scorers.append(MutualInfoScorer(task=approx_task, n_neighbors=3, random_state=0))
    if (selected is None or "dcor" in selected) and int(n_samples) <= int(dcor_max_n):
        scorers.append(DistanceCorrelationScorer())
    if selected is None or "perm" in selected:
        scorers.append(
            PermutationImportanceScorer(
                estimator=base_est,
                cv=int(perm_cv),
                n_repeats=int(perm_repeats),
                random_state=0,
                task=approx_task,
            )
        )
    return scorers


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch ~100 OpenML tabular datasets -> save CSV -> benchmark -> append experience.jsonl")
    ap.add_argument("--n", type=int, default=100, help="How many datasets to fetch/ingest")
    ap.add_argument("--seed", type=int, default=0, help="Random seed for picking datasets")
    ap.add_argument("--out-dir", default="data/openml_100", help="Where to save downloaded CSVs")
    ap.add_argument("--store", default="experience/experience.jsonl", help="Experience store JSONL path")
    ap.add_argument("--k", type=int, default=20, help="Top-k features used for evaluation")
    ap.add_argument("--cv", type=int, default=5, help="CV folds")
    ap.add_argument("--rf-estimators", type=int, default=300, help="RandomForest n_estimators for permutation importance scorer")
    ap.add_argument("--perm-cv", type=int, default=3, help="Permutation importance CV folds inside scorer")
    ap.add_argument("--perm-repeats", type=int, default=2, help="Permutation repeats (higher -> much slower)")
    ap.add_argument(
        "--methods",
        default="pearson,spearman,mi",
        help="Comma-separated subset of methods: pearson,spearman,mi,dcor,perm (default: pearson,spearman,mi).",
    )
    ap.add_argument(
        "--dcor-max-n",
        type=int,
        default=2000,
        help="Only run distance_correlation when n_samples <= this threshold (dCor is O(n^2 * p), very slow).",
    )
    ap.add_argument("--w-utility", type=float, default=1.0, help="w_u in objective J(A)")
    ap.add_argument("--w-stability", type=float, default=0.10, help="w_s in objective J(A)")
    ap.add_argument("--w-cost", type=float, default=0.15, help="w_c in objective J(A)")
    ap.add_argument("--max-instances", type=int, default=20000, help="Skip datasets larger than this many rows")
    ap.add_argument("--max-features", type=int, default=500, help="Skip datasets larger than this many features")
    ap.add_argument("--min-instances", type=int, default=50, help="Skip datasets smaller than this many rows")
    ap.add_argument("--min-features", type=int, default=2, help="Skip datasets smaller than this many features")
    ap.add_argument("--candidate-multiplier", type=int, default=10, help="Try a larger candidate pool to reach --n successful ingestions.")
    ap.add_argument("--retries", type=int, default=3, help="Retries for network operations (OpenML list/fetch).")
    ap.add_argument("--fail-log", default="experience/openml_failures.jsonl", help="Where to append failure records (JSONL).")
    ap.add_argument("--time-budget-sec", type=int, default=0, help="If >0, stop after this wall-clock budget (best-effort)")
    ap.add_argument("--no-progress", action="store_true", help="Disable progress output")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    store_path = Path(args.store)
    store = ExperienceStore(store_path)
    already = _already_ingested_dataset_ids(store_path)

    progress = _Progress(enabled=not args.no_progress)
    deadline = time.time() + float(args.time_budget_sec) if int(args.time_budget_sec) > 0 else None
    fail_log = Path(args.fail_log)
    manifest_path = out_dir / "manifest.jsonl"

    selected = _parse_methods(args.methods)
    if selected is not None:
        unknown = sorted(selected - {"pearson", "spearman", "mi", "dcor", "perm"})
        if unknown:
            raise SystemExit(f"未知 --methods 选项：{unknown}（支持 pearson,spearman,mi,dcor,perm）")

    def _pick():
        return _pick_candidate_ids(
            n=int(args.n),
            pool_multiplier=int(args.candidate_multiplier),
            seed=int(args.seed),
            max_instances=int(args.max_instances),
            max_features=int(args.max_features),
            min_instances=int(args.min_instances),
            min_features=int(args.min_features),
        )

    picked = _retry(_pick, retries=int(args.retries), progress=progress, ctx="[openml_list]")

    ingested = 0
    failed = 0

    for i, (data_id, name) in enumerate(picked, start=1):
        if deadline is not None and time.time() >= deadline:
            progress.done()
            print(f"\n达到 time budget，提前停止：已入库 {ingested} 个，失败 {failed} 个。")
            break
        if ingested >= int(args.n):
            progress.done()
            print(f"\n已达到目标入库数量：{ingested}/{int(args.n)}，提前停止。")
            break

        dataset_id = f"openml-{int(data_id)}"
        if dataset_id in already:
            progress.update(f"[{i}/{len(picked)}] skip already ingested: {dataset_id} ({name})")
            continue

        progress.update(f"[{i}/{len(picked)}] fetching: {dataset_id} ({name})")
        try:
            def _fetch():
                return fetch_openml(data_id=int(data_id), as_frame=True, parser="auto")

            bunch = _retry(_fetch, retries=int(args.retries), progress=progress, ctx=f"[fetch_openml {dataset_id}]")
            Xdf = bunch.data
            yser = bunch.target
            if isinstance(yser, pd.DataFrame):
                # 多 target 的情况：只取第一列
                yser = yser.iloc[:, 0]
            if yser is None:
                raise RuntimeError("OpenML dataset has no default target (skipping).")

            # 目标列名统一为 __target__，避免奇怪列名影响 CLI
            target_col = "__target__"
            Xdf = Xdf.copy()

            # 转换编码
            y_arr, y_meta = _encode_target(pd.Series(yser, name=target_col))
            X_num = _encode_tabular(pd.DataFrame(Xdf))

            # 保存 CSV（用户诉求：落盘 CSV）
            csv_path = out_dir / f"{dataset_id}.csv"
            df_save = Xdf.copy()
            df_save[target_col] = pd.Series(yser).astype("object")
            df_save.to_csv(csv_path, index=False)

            X = X_num.to_numpy(dtype=float, copy=False)
            y = np.asarray(y_arr)

            # 建 scorers
            approx_task, _, _ = infer_task(y)
            if approx_task == "classification":
                base_est = RandomForestClassifier(n_estimators=int(args.rf_estimators), random_state=0, n_jobs=-1)
            else:
                base_est = RandomForestRegressor(n_estimators=int(args.rf_estimators), random_state=0, n_jobs=-1)

            scorers = _build_scorers(
                selected=selected,
                approx_task=approx_task,
                base_est=base_est,
                n_samples=int(X.shape[0]),
                perm_cv=int(args.perm_cv),
                perm_repeats=int(args.perm_repeats),
                dcor_max_n=int(args.dcor_max_n),
            )
            if len(scorers) < 2:
                raise RuntimeError(f"selected methods too few: {selected} (need >=2)")

            meta = compute_dataset_meta_features(X, y).as_dict()
            meta["source"] = "openml"
            meta["openml_data_id"] = int(data_id)
            meta["openml_name"] = str(name)
            meta["csv_path"] = str(csv_path.as_posix())

            evaluations: Dict[str, Dict[str, Any]] = {}
            for mi, s in enumerate(scorers, start=1):
                progress.update(f"[{i}/{len(picked)}] {dataset_id} eval {mi}/{len(scorers)}: {s.name}")
                ev = evaluate_relevance_method(
                    X,
                    y,
                    s,
                    feature_names=list(X_num.columns),
                    k=int(args.k),
                    cv=int(args.cv),
                    scoring=None,
                    random_state=0,
                )
                ev_dict = ev.as_dict()
                ev_dict["objective_j"] = _objective_j(ev_dict, float(args.w_utility), float(args.w_stability), float(args.w_cost))
                evaluations[ev.method_name] = ev_dict

            # 选最优
            best_method = max(evaluations.items(), key=lambda kv: _objective_j(kv[1], args.w_utility, args.w_stability, args.w_cost))[0]
            best_metrics = evaluations[best_method]

            rec = ExperienceRecord(
                dataset_id=dataset_id,
                meta_features=meta,
                trajectory_features=None,
                evaluations=evaluations,
                selected_method=best_method,
                selection_reason={
                    "rule": "maximize J(A)=w_u*cv + w_s*stability - w_c*log(1+runtime)",
                    "weights": {"w_utility": args.w_utility, "w_stability": args.w_stability, "w_cost": args.w_cost},
                    "best_objective_j": _objective_j(best_metrics, args.w_utility, args.w_stability, args.w_cost),
                    "best_metrics": best_metrics,
                    "methods": list(evaluations.keys()),
                    "params": {
                        "k": int(args.k),
                        "cv": int(args.cv),
                        "rf_estimators": int(args.rf_estimators),
                        "perm_cv": int(args.perm_cv),
                        "perm_repeats": int(args.perm_repeats),
                    },
                    **y_meta,
                },
                created_at_utc=now_utc_iso(),
            )
            store.append(rec)
            already.add(dataset_id)
            ingested += 1
            _append_jsonl(
                manifest_path,
                {
                    "source": f"openml:{int(data_id)}",
                    "openml_data_id": int(data_id),
                    "openml_name": str(name),
                    "csv": str(csv_path.resolve()),
                    "target": target_col,
                    "task": approx_task,
                },
            )
            progress.update(f"[{i}/{len(picked)}] ingested: {dataset_id} best={best_method} (total={ingested})")
        except Exception as e:  # noqa: BLE001
            failed += 1
            progress.done()
            print(f"\nFAILED {dataset_id} ({name}): {type(e).__name__}: {e}")
            _append_jsonl(
                fail_log,
                {
                    "dataset_id": dataset_id,
                    "openml_data_id": int(data_id),
                    "openml_name": str(name),
                    "error_type": type(e).__name__,
                    "error": str(e),
                    "created_at_utc": now_utc_iso(),
                },
            )
            continue

    progress.done()
    print(f"done. ingested={ingested} failed={failed} store={store_path.resolve()} out_dir={out_dir.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


