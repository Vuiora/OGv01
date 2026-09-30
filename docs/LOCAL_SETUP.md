# 本地运行与配置迁移

## 独立环境

推荐 Python 3.12。各模块维持自己的 `pyproject.toml` 和依赖边界，进入相应目录再安装、测试和启动。CLC、Mentor 的声明限制不支持 Python 3.14；不要将系统默认 Python 当作所有模块通用环境。

原电脑 `.venv`、`node_modules`、Docling 模型、任务数据库与 n8n 凭据存于原工作区，不随 Git 克隆。`.env.example` 是新机器的配置起点。

CLC、CRD、架构工作台的端口和持久化目录分别依各自 README。部分原生脚本会复用同级模块依赖；保持本仓库目录布局后安装或显式指定依赖路径。

`n8n-Docling` 原生部署依赖工作区 `work/n8n-docling`。整合仓库发布的是流程源码，需要重新安装其说明中记录的 n8n/Docling，初始化凭据、发布工作流并调整 Obsidian 路径。

## Wiki 凭据

整合副本中的 `docker-compose.yml` 从本地 `.env` 读取以下变量，`LocalSettings.php` 通过容器环境读取同一配置。原工作区的现有 Wiki 配置仍保留原位。

```dotenv
OGWIKI_DB_ROOT_PASSWORD=在本地生成随机值
OGWIKI_DB_PASSWORD=在本地生成随机值
OGWIKI_SECRET_KEY=在本地生成随机值
OGWIKI_UPGRADE_KEY=在本地生成随机值
```

在 `Wiki/wiki-deploy` 运行 `py -3.12 init_local_env.py` 可生成 `.env`；文件已存在时拒绝覆盖。连接既有数据库时应填写其现有密码。Compose 保留原先的外部卷声明，需要已有 `build_dbdata` 与 `build_wikidata`，以及原站点要求的扩展和内容；当前配置不是完全独立的新站点安装器。

管理员密码在站点初始化时自行设置，文档中的 `CONFIGURE_LOCALLY` 是占位提示。机器人账号在 MediaWiki 内创建后，将密码写入本机 `botpw.txt`，权限要求以原 README 为准；该文件不纳入 Git。

转换与导入脚本中的原机器绝对路径应改为本机资料所在位置，再执行导入。迁移保留了既有内容与源码，没有重新调用模型、向 Wiki 写入页面或启动付费任务。
