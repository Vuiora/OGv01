# Git 历史整合方法

整合仓库：`Vuiora/OGv01`，私有。原工作区和 SDL、Mentor 原始 Git 仓库保留原位；MTBMT 从远端完整拉取到原工作区的 `MTBMT/`，随后合入整合仓库的同名目录。

## 保留范围

| 来源 | 原主分支 HEAD | 原提交数 | 整合后的历史分支 / 标签 |
| --- | --- | --- | --- |
| StatisticalDiscoveryLearning | `a7959a2c34bb12cfc283e07efaa07a4535c357a0` | 11 | `history/sdl/main` / `history/sdl/imported-head` |
| Mentor | `207251694b5a4b07951aa136d4d52f9674e454c6` | 3 | `history/mentor/main` / `history/mentor/imported-head` |
| MTBMT | `d9cb7f443a95846c8053b60c19a63c8a7100f223` | 40 | `history/mtbmt/main` / `history/mtbmt/imported-head`；另保留 `history/mtbmt/v0.1.0` 和 `history/mtbmt/v0.1.1` |

原始提交 SHA、作者、时间、消息和父链不改写。`main` 通过每个来源一个双父 merge 提交连接原始 HEAD；合并提交的树将原文件放在对应项目子目录，原始提交本身仍使用原仓库的根路径。

这种方式使 54 个原始提交都成为 `main` 的祖先，克隆默认分支即可取得它们。保留的历史分支与标签便于直接检出原项目版本。没有 squash，没有把子项目变成 submodule，也没有把其 `.git` 目录提交为普通文件。

## 操作顺序

1. 检查来源是否为完整历史，记录 HEAD、refs、提交集合和工作目录状态；检查历史 blob 中已知本地凭据及常见密钥模式、GitHub 单文件限制。
2. 在独立目录初始化 `main`，建立整合说明与工具。
3. 从本地仓库 fetch 分支和标签，分别映射至 `history/<来源>/...`。
4. `git read-tree --prefix=<项目路径>/ -u <原HEAD>` 将原树写入子目录。
5. `git commit-tree` 创建以当前 `main` 和原 HEAD 为父的合并提交，更新 `main`。
6. 导入当前工作目录源码，包含未提交的安全源文件；排除数据库、依赖、缓存、运行副本、私钥及本地 `.env`。
7. Wiki 的硬编码运行凭据改用环境变量，机器人密码文件仅保留本地；PRT 开发环境配置脚本的硬编码 API Key 改为环境变量 / 占位。所有修改记录源文件与导入文件的 SHA-256。
8. 运行迁移校验、既有离线测试，再推送 `main`、历史分支及标签；远端读取私有状态和分支 SHA 复核发布结果。

## 查询示例

```powershell
git log --graph --oneline --all
git log history/sdl/main
git log history/mentor/main
git log history/mtbmt/main
git show a7959a2:sdl_m11/driver.py
git merge-base --is-ancestor a7959a2 main
git merge-base --is-ancestor 2072516 main
git merge-base --is-ancestor d9cb7f4 main
py -3.12 tools/monorepo/import_workspace.py verify --repo .
```

查看原提交时使用原仓库路径，例如 `sdl_m11/driver.py`；查看合并后文件时使用 `F-20260919-StatisticalDiscoveryLearning/sdl_m11/driver.py`。子目录路径的 `git log -- <path>` 不会自动跨越这种路径迁移；应结合历史分支查询。

`migration-manifest.json` 保存来源 refs。Mentor 另有 Codex checkpoint ref，属于工具内部检查点，不是独立的用户分支；正式历史保留以 Git 提交祖先、来源分支及标签为准。

没有 Git 历史的项目在一次源码导入提交中建立起点。目录名里的日期只是来源信息，不伪造成过去的提交日期。原仓库未提交修改保存在整合后的导入提交中，原仓库 HEAD、索引和工作目录状态保持原样。

## 增量追加 MTBMT

使用新增的 `add` 子命令，在已有主线中连接完整来源历史，保留版本标签并追加文件散列记录。来源中的已跟踪数据样例按原 Git 内容纳入；运行时新增的缓存与经验输出继续受忽略规则约束。

```powershell
py -3.12 tools/monorepo/import_workspace.py add --source ../MTBMT --repo . --prefix MTBMT --id mtbmt
```

该操作已执行，不能对已存在的 `MTBMT/` 重复执行。来源与目标须为干净的完整仓库；导入前核对检出文件与原 Git 字节一致，扫描全部历史 blob，并对分支和标签分别加来源命名空间。
