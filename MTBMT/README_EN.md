## MTBMT (Meta-learning + Trajectory Guidance)

MTBMT (Mentor-Trajectory Based Model Trainer) focuses on two tracks:
**feature relevance/importance method selection** and **training-trajectory guidance**. It provides reproducible scripts and a unified JSONL experience store.

### What this repo does

- **Comparable relevance/importance benchmarking**: evaluates different scoring methods under a unified protocol (utility / stability / runtime).
- **Method selection (Meta Selector)**: writes **dataset meta-features + (optional) trajectory features + per-method evaluations** into an experience store, then trains/evaluates a meta-learner. The key metric is **Top-1 accuracy** under a dataset-wise split.
- **Trajectory guidance (Trajectory Guidance / Guided-CART)**:
  - **Trajectory Guidance**: iteratively adjusts `DecisionTreeClassifier` hyperparameters based on preferences over effect / trajectory length / trajectory retrieval time.
  - **Guided-CART**: generates split candidates at each node and uses a learned “tendency” model to **rerank/replace** candidates, weakening pure gini-driven decisions in a controllable way.
- **Data pipeline (OpenML ingestion)**: batch fetches OpenML tabular datasets → benchmarks methods → appends to the experience store (supports dedup / failure logs / retries / time budget).

### What you get

- **Unified experience format**: `experience/experience.jsonl` (JSONL; one record per line; appendable and reusable)
- **Comparable evaluations**: each method records `cv_score_mean/std`, `stability_jaccard`, `runtime_sec`, plus a configurable objective:

\[
J(A)=w_u\cdot \text{Utility}+w_s\cdot \text{Stability}-w_c\cdot \log(1+\text{Cost})
\]

- **Meta-selector evaluation**: strict `dataset_id`-wise splitting via `GroupKFold`, with `--aggregate-by-dataset` to reduce label noise when a dataset has multiple records.
- **Runnable Guided-CART implementation**: `rerank/replace` modes, `alpha` controls tendency strength, plus agreement/runtime analysis scripts.

### Docs

- `docs/feature_relevance_algorithm_selection.md`: method taxonomy, metrics, experience schema, and meta-eval protocol
- `docs/zh/guides/核心概念说明.md`: core concepts (trajectory/tendency/guidance) in Chinese

---

## Environment & installation

- **Python**: >= 3.10 (3.12+ recommended)

Install in editable mode (recommended so scripts can import `mtbmt`):

```bash
python -m pip install -e .
```

Optional RL demo dependencies (not required for core MTBMT features):

```bash
python -m pip install -r requirements-rl.txt
```

> Note: on many Linux distros, `python` may be `python3`. If `python` is not found, replace it with `python3` in the commands below.

---

## Quickstart: build an experience store from bundled CSVs

This repo ships with a set of ready-to-run classification datasets under `data/public10/*.csv` and `data/public10_v2/*.csv`.
Their target column is consistently **`label`** (see the corresponding `manifest.jsonl`).

Minimal end-to-end example (benchmark methods on one CSV + append to the store):

```bash
python scripts/benchmark_relevance.py \
  --csv data/public10/banknote_authentication.csv \
  --target label \
  --k 20 --cv 5 \
  --store experience/experience.jsonl
```

You should see:

- `selected_method: ...`
- `experience_appended_to: .../experience/experience.jsonl`

### Subset of methods (optional)

`--methods` accepts a comma-separated subset (default runs all): `pearson,spearman,mi,dcor,perm`

Example (run the faster trio only):

```bash
python scripts/benchmark_relevance.py \
  --csv data/public10/iris.csv --target label \
  --methods pearson,spearman,mi
```

### Note on feature column types

`benchmark_relevance.py` currently converts feature columns using `to_numeric(errors="coerce")`, so non-numeric values become NaN.
If your CSV has many categorical/string features, consider encoding upstream, or use the OpenML ingestion script below (it factorizes categorical features and targets).

---

## Batch: fetch OpenML and ingest (CSV saved + store appended)

`scripts/fetch_openml_100_and_ingest.py` will:

- pick candidate datasets from OpenML list (with size filters)
- download and save CSVs into `--out-dir`
- lightly encode features/targets (categoricals via factorize)
- benchmark selected methods and append to the experience store
- write `manifest.jsonl` and failures to `experience/openml_failures.jsonl`

Example (smoke-scale first to keep runtime low):

```bash
python scripts/fetch_openml_100_and_ingest.py \
  --n 10 \
  --out-dir data/openml_100 \
  --store experience/experience.jsonl \
  --methods pearson,spearman,mi \
  --cv 5 --k 20
```

---

## Meta-selector evaluation (Top-1/Top-N + regret + time savings)

Entry point: `scripts/evaluate_meta_selector.py`

It builds a supervised dataset from the experience store:

- Features: flattened `meta_features` + (if present) `trajectory_features` prefixed with `traj__`
- Label: the “best method” per record (`--label-target objective|cv`)
- Split: dataset-wise group split by `dataset_id` (same dataset never crosses train/test)

Make sure you have enough distinct `dataset_id` values (at least 3) before evaluating. Example:

```bash
python scripts/evaluate_meta_selector.py \
  --experience experience/experience.jsonl \
  --cv-datasets 5 \
  --aggregate-by-dataset \
  --topn 2
```

---

## Trajectory Guidance: iteratively correct decision-tree hyperparameters

Minimal demo on synthetic data (trades off effect / trajectory length / trajectory time):

```bash
python scripts/trajectory/guide_decision_tree.py \
  --w-effect 1.0 --w-length 0.05 --w-time 0.10 \
  --iters 6 --traj-samples 512
```

---

## Guided-CART: rerank/replace split candidates

Demo (compare sklearn CART vs Guided-CART):

```bash
python scripts/trajectory/guided_cart_demo.py \
  --mode rerank \
  --alpha 1.0 \
  --depth 8 \
  --shortlist-k 8 \
  --threshold-strategy unique_midpoints_cap \
  --thresholds 64 \
  --reranker-target val_gain
```

More scripts:

- `scripts/trajectory/compare_cart_guided_agreement.py`: multi-seed agreement comparison
- `scripts/trajectory/benchmark_guided_cart_runtime.py`: runtime breakdown (warmup/repeats)
- `scripts/trajectory/analyze_guided_cart_alpha.py`: analyze how `alpha` changes splits

---

## Experience store (JSONL) schema

The schema is defined in `src/mtbmt/experience_store.py`. One line per record, key fields:

- `dataset_id`: dataset identifier (local CSV uses `stem + hash`; OpenML uses `openml-<id>`)
- `meta_features`: dataset meta-features from `src/mtbmt/meta_features.py`
- `trajectory_features`: optional (benchmark scripts default to `null`; you can import your own)
- `evaluations`: `method_name -> metrics` including `cv_score_mean/std`, `stability_jaccard`, `runtime_sec` (and extra fields like `objective_j`)
- `selected_method`: chosen best method for the record
- `selection_reason`: interpretable selection details (rule/weights/params)
- `created_at_utc`: UTC ISO8601 timestamp

---

## Tests

```bash
python -m pip install -e .
python -m pip install pytest
python -m pytest -q
```

