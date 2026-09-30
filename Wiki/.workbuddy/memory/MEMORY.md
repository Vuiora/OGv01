# MEMORY.md — OGWiki 项目

## 项目定位
`C:/Users/Lenovo/Desktop/OGv01/Wiki`：本地 MediaWiki 知识库（http://localhost:8080），
现收录**四套**知识体系：
1. **计算机数学基础** —— 源自课程讲义（71 张 PPT 幻灯片 PNG），18 页。
2. **动手学深度学习（D2L）** —— 源自开源教材 PyTorch 版，141 页（20 章 + 121 节 + 总索引）。
3. **操作系统导论（OSTEP）** —— 源自中文 PDF（500 页），59 页（前言 + 第1–50章 + 附录A–G + 总览）。
4. **图论（Diestel 第五版）** —— 源自中文 PDF（412 页），19 页（前言 + 12 章 + 附录 + 总览）。

## 交付物
- `wiki-deploy/` — 完整可复现的 Docker 部署（mediawiki:1.43 + mariadb:11），
  站点 http://localhost:8080，详见该目录 README.md。
- `wiki-deploy/pages/*.wiki` — 课程知识库 18 个页面（唯一事实来源）。
- `wiki-deploy/d2l/` — D2L 导入套件：`pageindex.json`、`pages/*.wiki`（141 页）、
  `md2wiki.py`、`import_d2l.py`、`verify_all.py`、`README.md`（含全部转换踩坑）。
- `wiki-deploy/book2wiki/` — 中文 PDF 教材导入套件：`ostep/pages`（59）、
  `graphtheory/pages`（19）、`ostep/img`（297 插图）、`graphtheory/img`（106 插图）、
  `struct_*.json`、`code_pdf.json`、`scripts/`
  （ocr_pdf/pdfcode/structure/layout/media/book2wiki/import_book/upload_figs/mk_sheets/
  verify_books/audit）、`README.md`（含全部 PDF+OCR+插图提取踩坑）。
- `wiki-deploy/uploads.ini` — PHP 上传限制提至 32M（含 compose 挂载 + `$wgMaxUploadSize`）。
- `.build/` — 过程产物（OCR 缓存 jsonl、源 PDF、分析脚本、截图）。可清理。

## 约定
- 页面组织：**按知识主题拆分多页**，用分类 + 互链（`参见`节）串联；不按“一页一讲义”。
- 主页 `首页` 作为总索引，展示四套体系；每套按章分类。
- 导入方式：经 MediaWiki API（bot 账号）写入，脚本幂等可重跑。
- 修改页面的正确流程：改对应 `pages/*.wiki` → 校验 → 导入。

## 关键约束（勿违反）
1. **Bot 密码字符集**：MediaWiki 要求 bot 密码匹配 `^[0-9a-w]{32,}$`，否则不走 bot
   登录分支、报笼统的 `wrongpassword`（即使哈希 verify=true）。当前密码存
   `wiki-deploy/botpw.txt`，不要改成含大写/x/y/z/连字符的字符。
2. **模板用 HTML `<table>`**，不要用 wikitext `{|`——后者配合 `{{#if}}` 会生成空
   `<p><br/></p>`。
3. 容器名固定 `ogwiki-web` / `ogwiki-db`；数据卷 `build_wikidata` / `build_dbdata`
   声明为 external，便于从任意 compose 目录接管。
4. **数学渲染用 native 模式**（`$wgMathValidModes=['native','source']`）——本机
   访问不到 wikimedia.org 的 RESTBase，MathML 模式会产生失败回退。
5. **D2L 转换**：`(**..**)`/`[**..**]` 可跨行；`:eqlabel:` 须纳入 label 映射；
   无 pytorch/all 的 tab 代码组必须连围栏一并消费（详见 `d2l/README.md`）。
6. **PDF 教材（book2wiki）必须先探测文字层再决定策略**：中文 PDF 常见「真文字层但
   CMap 损坏」（OSTEP 正文）或「隐藏低质 OCR 层」（图论）；但**等宽字体代码层通常干净**，
   应直接取文字层而非 OCR。详见 `book2wiki/README.md`。
7. **OCR 性能**：`OMP_NUM_THREADS=1` 必须在 `import onnxruntime` 之前设置，
   否则多进程×多线程超订（ETA 13 分钟 → 2.4 小时）。
8. **插图提取方向规则**（`media.py`，勿改错）：「图 N.M」图注在图形**下方**→ 向上扩展；
   「表 N.M」表题在表格**上方**→ 向下扩展。不能只比两侧长度（会吞掉邻图）。
9. **代码清单行判定**：含「1-3 位窄行号片段」或「CJK 占比极低（≤15%）」且含代码记号的行
   属代码，是**图形内容**而非正文——扩展图形区时不得把它当边框中断。
10. **MediaWiki 文件名折叠连续下划线**：`bookimg__gt_p017_0` → `Bookimg_gt_p017_0`。
    比对文件名/校验 File 引用前必须归一化，否则会把全部引用误报为缺失。
11. **bot 密码删除文件**：删 File 需 bot 密码含 `delete` grant（`bp_grants`），
    否则报 permissiondenied；页面编辑/上传不需要。
12. **视觉审查用 agent 集群**：`mk_sheets.py` 生成 contact sheet（每张 16 图、宽 300px、
    100–400KB），派并行 subagent 读图分类问题。**sheet 过大（>5MB）会触发 413**。
