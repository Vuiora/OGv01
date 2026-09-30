# OGWiki 部署说明

本目录包含一套可复现的 MediaWiki 部署配置，用于承载「计算机数学基础」课程知识库。

## 快速启动

```bash
cd wiki-deploy
docker compose up -d
```

启动后访问：<http://localhost:8080>

## 架构

| 组件 | 镜像 | 说明 |
|---|---|---|
| ogwiki-web | mediawiki:1.43 | MediaWiki 1.43.9（Apache + PHP 8.3） |
| ogwiki-db | mariadb:11 | 数据库，utf8mb4 字符集 |

数据持久化在两个 Docker 卷：`build_wikidata`（程序与上传文件）、`build_dbdata`（数据库）。

## 账号

| 用途 | 用户名 | 密码 |
|---|---|---|
| 管理员（网页登录） | `Admin` | `CONFIGURE_LOCALLY` |
| 机器人（API 写入） | `Admin@ogwikiimport` | 见 `botpw.txt` |

> 机器人密码已按 MediaWiki 约束生成：长度 ≥32，且字符集限定为 `[0-9a-w]`。
> 注意：MediaWiki 的 `BotPassword::canonicalizeLoginData()` 会校验密码字符集，
> 含大写字母、`x`/`y`/`z` 或连字符的密码会被拒绝并退化为主账号认证（报 wrongpassword）。
> 因此 `botpw.txt` 中的密码**不可随意修改**为任意字符串。

## 目录结构

```
wiki-deploy/
├── docker-compose.yml     # 服务编排
├── LocalSettings.php      # MediaWiki 配置（挂载为只读）
├── pages/                 # 知识条目的 wikitext 源文件
├── import_pages.py        # 通过 API 批量导入 pages/ 中的条目
├── lint_wikitext.py       # 离线语法校验（花括号/标签/表格/标题/分类）
├── botpw.txt              # 机器人密码
└── README.md
```

## 重新导入知识条目

```bash
python lint_wikitext.py      # 先做语法校验
python import_pages.py       # 再通过 API 写入（幂等，可重复执行）
```

文件名 → 页面标题的映射规则：`X.wiki` → 页面 `X`；`Template_Y.wiki` → `Template:Y`。

## 知识结构

导入的知识条目共 18 个页面（16 条知识条目 + 2 个模板）：

* **数学体系**：数学体系、数学结构
* **数学基础**：数学基础、数学悖论、三次数学危机、罗素悖论、理发师悖论、哥德尔不完全性定理
* **形式系统与数理逻辑**：形式系统、数理逻辑、命题逻辑、范式、推理理论、谓词逻辑
* **导航**：首页、计算机数学基础

分类：`计算机数学基础`、`数学体系`、`数学基础`、`数理逻辑`、`悖论`、`数学史`、`定理`、`模板`、`索引`。

## 已启用扩展

ParserFunctions、Math（MathML 公式渲染）、CategoryTree、Cite、SyntaxHighlight_GeSHi、
InputBox、Poem、WikiEditor、CodeEditor、Scribunto、TemplateData。

## 常用运维命令

```bash
docker compose ps                      # 查看状态
docker compose logs -f wiki            # 查看日志
docker compose down                    # 停止（保留数据）
docker compose down -v                 # 停止并删除数据（危险）

# 进入容器执行维护脚本
docker exec ogwiki-web php maintenance/run.php <script>

# 新建机器人密码
docker exec ogwiki-web php maintenance/run.php createBotPassword \
  --appid=<appid> --grants=createeditmovepage,editpage,uploadfile,highvolume,basic \
  <用户名> <密码>
```

## 备注

* 站点当前绑定 `http://localhost:8080`。若需供局域网/外网访问，需修改
  `LocalSettings.php` 中的 `$wgServer` 与 `$wgCanonicalServer`。
* 上传目录位于卷内的 `/var/www/html/images`，`$wgEnableUploads` 已开启。
