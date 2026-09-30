# 整合验证记录

日期：2026-09-30。所有测试在整合后的目录中运行，原项目复用已安装的环境，追加的 MTBMT 使用独立 Python 3.12 环境；没有调用真实模型服务或启动付费任务。

## 仓库与文件完整性

- 来源文件 1,224 个，共 180,181,812 bytes（约 171.83 MiB），最大单文件约 10.19 MiB。
- SDL 的 11 个原始提交、Mentor 的 3 个原始提交及 MTBMT 的 40 个原始提交，共 54 个提交均可从 `main` 沿祖先关系访问；原始 SHA、作者、时间与父链保留。
- 每个来源文件保存源文件和导入文件的 SHA-256；迁移工具逐文件校验，并确认来源文件都在 Git 索引中。
- `git fsck --full --no-dangling` 通过；原仓库 HEAD 及 Git 工作目录状态与导入前一致。
- 新增主线说明、项目清单和运行说明中的本地链接全部检查通过。
- 发布目标 `Vuiora/OGv01` 已通过 GitHub API 确认为 `PRIVATE`；默认主分支为 `main`。

精确清单见 [migration-manifest.json](migration-manifest.json)。所有未初始化项目均只保留状态说明，未伪造实现或旧提交。

## 凭据与配置检查

扫描了来源历史中的 blob、待导入源码、已知本地配置凭据及常见 API/GitHub token、私钥模式；SDL 凭据脱敏单元测试中的一个明确测试值按内容散列登记为例外。

以下 6 个来源文件在整合副本中做了凭据清理，散列差异已登记：

| 文件 | 处理 |
| --- | --- |
| `F-260903-PRT/setup-codexzh-mac.sh` | 移除硬编码 Key，读取 `CODEXZH_API_KEY` |
| `F-260903-PRT/setup-codexzh.ps1` | 移除硬编码 Key，沿用环境变量与缺失检查 |
| `F-260908-GPTSelf-renew/renew.validation.config.json` | 明文 `api_key` 改为 `null`，保留 `api_key_env` |
| `Wiki/wiki-deploy/docker-compose.yml` | 数据库密码和 Wiki 密钥通过本地环境配置 |
| `Wiki/wiki-deploy/LocalSettings.php` | 数据库密码、站点密钥与升级密钥从容器环境读取 |
| `Wiki/wiki-deploy/README.md` | 管理员明文密码改为本地配置占位 |

PRT Bash 与 PowerShell 脚本语法检查通过；Wiki Compose YAML 能解析，环境变量到 PHP 的对应关系已检查；Self Renew 配置的明文 Key 已确认移除。实际 `.env`、`botpw.txt`、数据库、角色 token 与依赖缓存均未纳入发布。上述检查的范围是已知值与明确模式，不代表对任意未知敏感内容的完备识别。

## 既有离线测试

| 项目 | 环境 | 用例数 | 结果 |
| --- | --- | ---: | --- |
| SDL | Python 3.12，标准库 | 1,747 | 1,745 通过，2 失败；耗时约 208 秒 |
| PLDA | Python 3.12，标准库 | 29 | 27 通过，1 失败，1 跳过（可选 NumPy 未安装） |
| Self Renew | Python 3.12，标准库 | 100 | 99 通过，1 跳过 |
| ConceptLayerConstructor | 原项目 Python 3.12 环境 | 23 | 全部通过 |
| ConceptRelationDraw | 原项目 Python 3.14 环境 | 48 | 全部通过 |
| ThePicWorkingFlow | 原项目 Python 3.14 环境 | 19 | 全部通过 |
| MTBMT | 独立 Python 3.12 环境 | 5 | 全部通过；另完成自带 Iris 数据的最小 Pearson 评测与 JSONL 经验写入 |

总计 1,971 个用例：1,966 通过、3 失败、2 跳过。追加 MTBMT 时，既有模块的来源文件未改动，未重复运行其测试；迁移校验与配置检查另计。Mentor 没有现成测试目录，且缺完整运行入口，未宣称端到端验收通过；GPU/NPU、Docker 部署和真实模型调用未在本次重复验证。

MTBMT 追加导入 124 个来源文件，并保留其已有跟踪的 CSV 样例；新下载缓存与运行时经验输出仍受忽略规则约束。其 199 个历史 blob 的凭据与大小检查通过，版本标签与完整历史已保留。具体范围与运行入口见 [MTBMT 整合说明](MTBMT_INTEGRATION.md)。

## 原项目已存在的失败

下面三项都在原项目目录中，用相同 Python 3.12 运行定点测试复现；迁移后的来源业务代码与原文件一致。

1. SDL `tests/test_protocol.py`：`test_client_role_cannot_be_changed_to_bypass_authorization`。客户端角色修改时没有抛出测试所要求的 `AttributeError` 或 `AccessDenied`。这是需另行处理的授权契约问题。
2. SDL `tests/test_protocol.py`：`test_failed_consumption_after_digest_check_remains_used_and_is_logged`。确证消费失败后，测试所期望的账本记录未出现。这是需另行处理的审计契约问题。
3. PLDA `tests/test_runtime.py`：`test_real_process_parallelism_and_dependency_commit`。后继预留与前置提交记录的时间戳相等，未满足测试的严格大于断言。同一用例在已有 Python 3.14 项目环境中通过，表现与环境有关；该复测不等于整个 Python 3.14 测试集的通过声明。

完整测试日志保留在本机 `.migration-local/stdlib-tests.log`，该日志没有纳入 Git。以上是原有测试基线，不将失败计为通过，也未在迁移中改变推理算法或修补授权逻辑。

## 复核命令

```powershell
py -3.12 tools/monorepo/import_workspace.py verify --repo .
git log --graph --oneline --all
git merge-base --is-ancestor a7959a2 main
git merge-base --is-ancestor 2072516 main
git merge-base --is-ancestor d9cb7f4 main
gh repo view Vuiora/OGv01 --json isPrivate,defaultBranchRef,url
git ls-remote origin refs/heads/main refs/heads/history/sdl/main refs/heads/history/mentor/main refs/heads/history/mtbmt/main
```

`check_projects.py --stdlib` 当前会以非零退出，因为它如实汇总上述基线失败。使用各项目独立环境可避免依赖或模块名混用。
