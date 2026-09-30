# MTBMT 整合记录

日期：2026-09-30。

## 来源与历史

来源为 [Vuiora/MTBMT-Mentor-TrajectaryBasedModelTrainer](https://github.com/Vuiora/MTBMT-Mentor-TrajectaryBasedModelTrainer)，完整历史 40 个提交，HEAD 为 `d9cb7f443a95846c8053b60c19a63c8a7100f223`。

整合到 `MTBMT/`，新增 124 个文件，共 32,781,997 bytes（约 31.26 MiB）。源码、已跟踪的 CSV 样例、说明和 MIT 许可证均保留，与原 Git 内容逐字节一致。

历史分支为 `history/mtbmt/main`；原版本标签映射为 `history/mtbmt/v0.1.0`、`history/mtbmt/v0.1.1`，另保存 `history/mtbmt/imported-head`。合并提交以原 HEAD 为父，保留原始 SHA、作者、时间、消息和祖先链。

导入前扫描了全部 199 个历史 blob，检查已知本地凭据及常见密钥模式，没有匹配。所有原始提交已验证为整合后 `main` 的祖先，文件散列及来源 refs 见 [迁移清单](migration-manifest.json)。

## 项目主线与实现

MTBMT 的主线为：数据与训练轨迹 → 相关性／重要性评测 → 经验特征 → 元学习算法选择与训练指导 → 评估和经验积累。

| 入口 | 职责 |
| --- | --- |
| `MTBMT/src/mtbmt/relevance/`、`evaluation.py` | 统一量化方法、下游效用、稳定性和耗时评测 |
| `experience_store.py`、`meta_features.py` | JSONL 经验记录与数据集元特征 |
| `meta_learner.py`、`meta_eval.py` | 随机森林元选择器与按数据集分组评估 |
| `trajectory_*`、`decision_tree_trajectory.py` | 轨迹特征提取与训练参数指导 |
| `guided_cart.py`、`guided_id3.py` | 节点分裂候选重排与替换 |
| `hpo/guided_asha.py`、`scripts/rl/`、`scripts/dkt/` | 资源分配、强化学习和分组 BKT-like 实验 |

基准经验记录默认不自动采集轨迹，需由对应训练过程提供。后续新增的 `ogflow/recommendation.py` 已将 E 元特征、三种相关性方法、分组 CV 和历史随机森林推荐接入 SDL 探索侧；按当前数据集和来源散列排除经验，标签只由 E 评价产生。详细运行与接入边界见 [业务说明](BUSINESS_CLOSED_LOOP.md)。MTBMT 与 `F-20260914-Utils/Mentor` 分别维护训练策略和 Teacher/Student 应用构建方向。

## 本次验证

独立 Python 3.12 环境安装来源声明的依赖：NumPy 2.5.3、pandas 3.0.6、scikit-learn 1.9.1、pytest 9.1.1。

五项现有测试全部通过，覆盖轨迹特征维度、原始列保留与轨迹标签。使用自带 `data/public10/iris.csv` 完成 Pearson 相关性最小评测（`k=2`、`cv=2`），成功产生评价指标并写入一条 JSONL 经验记录。

这次验证覆盖本地测试与最小评测链条；完整元学习训练、Guided-CART/ID3、RL/HPO 和分组 BKT-like 的效果未在本次重复评估。依赖环境及烟测输出位于本机 `.migration-local/`，不纳入 Git。

## 运行

```powershell
cd MTBMT
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e . -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
```

整合仓库根目录提供统一测试入口：

```powershell
py -3.12 tools/monorepo/check_projects.py --project mtbmt --python MTBMT/.venv/Scripts/python.exe
```

新增的运行输出和下载缓存沿用项目忽略规则；已跟踪的来源数据样例继续随版本保存。其他运行与实验参数见 [原 README](../MTBMT/README.md)。
