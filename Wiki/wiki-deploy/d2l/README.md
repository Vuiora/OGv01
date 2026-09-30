# D2L 知识库导入套件

把《动手学深度学习》（Dive into Deep Learning, PyTorch 版）导入本地 MediaWiki。

## 数据来源

- **教材 PDF**：`C:\Users\Lenovo\Downloads\d2l-zh-pytorch.pdf`（797 页）
- **转换源**：官方仓库 `d2l-ai/d2l-zh`（master 分支）的 Markdown 源码。
  选择 Markdown 而非 PDF 抽取的原因：PDF 的数学公式被拆成 Type1 字体碎片
  （CMR10/CMMI10/CMSY7），无法还原为 LaTeX；而 Markdown 源码中公式是完整
  的 LaTeX，代码块保留缩进，质量远高。

> 注：仓库 master 为四框架并存（MXNet/PyTorch/TensorFlow/Paddle），
> 用 `#@tab` 标记；本套件按需求**只提取 PyTorch 实现**。

## 目录结构

```
d2l/
├── md2wiki.py       # Markdown -> Wikitext 转换器（含 label 解析、tab 提取）
├── import_d2l.py    # 上传图片 + 写入页面（幂等可重跑）
├── verify_all.py    # 全站验证（页面数 / 红链 / 分类）
├── pageindex.json   # 文件 stem -> 页面标题 映射
└── pages/*.wiki     # 141 个转换后的 wikitext 源文件（唯一事实来源）
```

`.build/d2l/` 下另有过程产物：`tree.json`、`src/`（141 个源 md）、
`img/`（184 张图片）、各阶段分析脚本。可清理。

## 复用步骤

```bash
PY="C:/Users/Lenovo/.workbuddy/binaries/python/envs/default/Scripts/python.exe"

# 1) 拉取源（需能访问 raw.githubusercontent.com）
$PY .build/d2l/fetch_src.py      # 141 个 md -> .build/d2l/src/
$PY .build/d2l/fetch_img.py      # 184 张图 -> .build/d2l/img/（文件名加 img__ 前缀）

# 2) 转换（改完 pages/ 后重跑即可）
cd .build/d2l && $PY md2wiki.py   # -> .build/d2l/pages/*.wiki

# 3) 导入
$PY .build/d2l/import_d2l.py      # 先传图，再写页

# 4) 验证
$PY .build/d2l/verify_all.py
```

## 转换器要点（踩过的坑）

1. **tab 分组**：连续代码块（之间仅空白）构成一组，组内取含 `pytorch` 的块，
   否则取 `all`；**整组无 pytorch/all 时必须消费掉围栏**，否则围栏残留并被
   后续行内正则误渲染（曾导致 `(**kwargs)` 跨段落误匹配、框架代码泄漏）。
2. **`(**...**)` 与 `[**...**]`** 是 d2l 的重点标记，**允许跨行**；须先屏蔽
   `<syntaxhighlight>` 内容再处理行内，否则代码里的 `(**kwargs)` 会被误转。
3. **`:begin_tab:` 文本块**按框架分组去重，只留 pytorch 分支（块间有空行，须跳过）。
4. **`:eqlabel:` 也是 label**，须与 `:label:` 一并纳入映射，否则 `:eqref:` 失效。
5. **页面重名**：`chapter_multilayer-perceptrons__index` 与 `__mlp` 都叫「多层感知机」，
   用 `TITLE_OVERRIDE` 消歧为「多层感知机（章）」。标题含 `<code>` 的页面须覆盖为纯文本。
6. **公式渲染**：`LocalSettings.php` 用 `native` 模式（PHP 本地 LaTeX→MathML），
   本机无法访问 wikimedia.org 的 RESTBase。已验证 aligned/bmatrix/cases 环境
   与 \mathbf/\mathbb/\mathcal/\boldsymbol/\underbrace 等命令全部可用。
7. **上传限制**：`twogpu.svg`（2.8MB）超出默认 2MB，需 `uploads.ini` + `$wgMaxUploadSize`。

## 成果

- 141 个页面（20 章 + 121 内容节 + 总索引），零红链
- 4254 个 MathML 公式，979 个 PyTorch 代码块，184 张插图
- 按章节分为 20 个分类，统一挂在 `Category:动手学深度学习` 下
