## MTBMT（Meta-learning + Trajectory Guidance）

MTBMT（Mentor-Trajectory Based Model Trainer）围绕“**特征相关性/重要性量化算法选择**”与“**训练轨迹引导**”两条主线，提供一套可复现实验脚本与统一经验库（JSONL）沉淀机制。

English version: `README_EN.md`

### 本仓库解决什么问题

- **相关性/重要性量化可比较**：用统一评测口径（Utility/稳定性/耗时）把不同方法放到同一尺度下比较。
- **算法选择（Meta Selector）**：把 **数据集元特征 +（可选）轨迹特征 + 各方法评估结果** 写入经验库，再训练/评估元学习选择器，核心指标是 **Top1 accuracy**（dataset-wise 分组切分）。
- **轨迹指导（Trajectory Guidance / Guided-CART）**：
  - **Trajectory Guidance**：用“效果/轨迹长度/轨迹提取时间”的偏好参数，迭代调整 `DecisionTreeClassifier` 超参。
  - **Guided-CART**：在每个节点生成候选分裂，再用倾向模型对候选 **rerank/replace**，从“下一分裂决策”层面弱化纯 gini 驱动，实现可控的轨迹引导。
- **数据管道（OpenML 批量入库）**：批量抓取 OpenML 表格数据集 → 跑基准评测 → 追加写入经验库（支持断点/去重/失败日志/重试/时间预算）。

### 你将得到什么

- **统一经验格式**：`experience/experience.jsonl`（JSONL，一行一条，可持续追加与复用）
- **可比较量化**：每个方法记录 `cv_score_mean/std`、`stability_jaccard`、`runtime_sec`，并支持综合目标：

\[
J(A)=w_u\cdot \text{Utility}+w_s\cdot \text{Stability}-w_c\cdot \log(1+\text{Cost})
\]

- **元选择器评估**：严格按 `dataset_id` 分组切分（`GroupKFold`），并支持 `--aggregate-by-dataset` 缓解“同一 dataset 多条记录导致 label 噪声”。
- **Guided-CART 可运行实现**：`rerank/replace` 两种模式、`alpha` 控制倾向强度，配套一致率/耗时对比脚本。

### 文档

- `docs/feature_relevance_algorithm_selection.md`：算法谱系、评价指标、经验库与元学习评估口径
- `docs/zh/guides/核心概念说明.md`：轨迹/倾向/指导等核心概念

---

## 环境与安装

- **Python**：>= 3.10（推荐 3.12+）

推荐以可编辑方式安装，避免脚本找不到 `mtbmt`：

```bash
python -m pip install -e .
```

（可选）RL 相关 demo 依赖不属于核心功能：

```bash
python -m pip install -r requirements-rl.txt
```

---

## 快速开始：从仓库自带 CSV 生成经验库

仓库内置了一批可直接跑的分类数据集：`data/public10/*.csv`、`data/public10_v2/*.csv`，它们的目标列统一为 **`label`**（见对应 `manifest.jsonl`）。

最小闭环（对单个 CSV 做基准评测 + 写入经验库）：

```bash
python scripts/benchmark_relevance.py \
  --csv data/public10/banknote_authentication.csv \
  --target label \
  --k 20 --cv 5 \
  --store experience/experience.jsonl
```

你会在终端看到：

- `selected_method: ...`
- `experience_appended_to: .../experience/experience.jsonl`

### 方法子集（可选）

`--methods` 支持逗号分隔子集（默认全跑）：`pearson,spearman,mi,dcor,perm`

示例（只跑更快的三种）：

```bash
python scripts/benchmark_relevance.py \
  --csv data/public10/iris.csv --target label \
  --methods pearson,spearman,mi
```

### 注意：特征列类型

`benchmark_relevance.py` 目前会对特征列做 `to_numeric(errors="coerce")`，非数值会变成 NaN。若你的 CSV 含大量类别/字符串特征，建议先做编码，或直接使用下面的 OpenML 入库脚本（该脚本会对特征/目标做 factorize 编码）。

---

## 批量：抓取 OpenML 并入库（CSV 落盘 + 经验库追加）

脚本 `scripts/fetch_openml_100_and_ingest.py` 会：

- 从 OpenML 列表中挑选候选数据集（按规模过滤）
- 下载并保存 CSV 到 `--out-dir`
- 对特征/目标做轻量编码（类别 factorize）
- 对指定 methods 跑统一评测并追加写入经验库
- 记录 `manifest.jsonl` 与失败日志 `experience/openml_failures.jsonl`

示例（先做 smoke 规模，避免跑太久）：

```bash
python scripts/fetch_openml_100_and_ingest.py \
  --n 10 \
  --out-dir data/openml_100 \
  --store experience/experience.jsonl \
  --methods pearson,spearman,mi \
  --cv 5 --k 20
```

---

## 元学习选择器评估（Top1/TopN + regret + 时间节省）

入口：`scripts/evaluate_meta_selector.py`

该脚本会从经验库构建训练集：

- 输入特征：`meta_features` 扁平化 +（若存在）`trajectory_features`（以 `traj__` 前缀拼接）
- 标签：每条经验记录的“最优方法”（可选按 `--label-target objective|cv`）
- 切分：按 `dataset_id` 分组（同一 dataset 不跨 train/test）

建议先用多个不同 CSV 生成足够的 `dataset_id`（至少 3 个），再做评估。示例：

```bash
python scripts/evaluate_meta_selector.py \
  --experience experience/experience.jsonl \
  --cv-datasets 5 \
  --aggregate-by-dataset \
  --topn 2
```

---

## 轨迹指导（Trajectory Guidance）：迭代纠偏决策树超参

最小示例（合成数据上演示“效果/轨迹长度/轨迹时间”的偏好权衡）：

```bash
python scripts/trajectory/guide_decision_tree.py \
  --w-effect 1.0 --w-length 0.05 --w-time 0.10 \
  --iters 6 --traj-samples 512
```

---

## Guided-CART：分裂候选的 rerank/replace

Demo（对比 sklearn CART vs Guided-CART）：

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

更多实验脚本：

- `scripts/trajectory/compare_cart_guided_agreement.py`：多 seed 一致率对比
- `scripts/trajectory/benchmark_guided_cart_runtime.py`：耗时拆分统计（含 warmup/repeats）
- `scripts/trajectory/analyze_guided_cart_alpha.py`：分析 `alpha` 对 split 变化的影响

---

## 经验库（JSONL）字段说明

经验库由 `src/mtbmt/experience_store.py` 定义，一行一条记录，核心字段如下：

- `dataset_id`: 数据集唯一标识（本地 CSV 会用“文件名 + hash”，OpenML 用 `openml-<id>`）
- `meta_features`: `src/mtbmt/meta_features.py` 产出的数据集元特征
- `trajectory_features`: 可选（当前基准脚本默认 `null`；如你有训练日志可自行导入）
- `evaluations`: `method_name -> metrics`，包含 `cv_score_mean/std`、`stability_jaccard`、`runtime_sec`（以及 `objective_j` 等附加项）
- `selected_method`: 本条记录选择的最优方法
- `selection_reason`: 选择的规则/权重/参数等可解释信息
- `created_at_utc`: 写入时间（UTC ISO8601）

---

## 测试

```bash
python -m pip install -e .
python -m pip install pytest
python -m pytest -q
```