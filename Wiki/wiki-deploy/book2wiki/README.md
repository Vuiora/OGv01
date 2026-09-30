# book2wiki —— 中文 PDF 教材 → MediaWiki 导入套件

把**两本中文 PDF 教材**转换为 MediaWiki 知识条目并导入本地 wiki：

| 书 | 页数 | 条目 | 页码→结构 |
|---|---|---|---|
| 《操作系统导论（三座大山中文版）》OSTEP | 500 | 59（前言 + 第1–50章 + 附录A–G + 总览页） | 一章一页 |
| 《图论》Diestel 第五版（于青林 译） | 412 | 19（前言与译者序 + 12 章 + 附录A/B + 提示/索引/书目 + 总览页） | 一章一页，章内小节用二级标题 |

站点：<http://localhost:8080>　分类：`操作系统导论`、`图论`（各章另有子分类）

---

## 一、为什么不能直接抽文字层（关键侦察结论）

两本书的 PDF 生成方式完全不同，**必须先探测再决定策略**：

| 书 | 文字层情况 | 处理 |
|---|---|---|
| **OSTEP** | 内嵌真实文字层：正文 `SimSun`（约 30 万汉字）、代码 `CourierNew`（约 7.5 万字符）。但 **ToUnicode CMap 有系统性错字**：`时→谁`、`们→我`。标题用子集字体 `FZ*` 同样损坏。 | 正文/标题走 **OCR**；**代码块直接取 `CourierNew` 文字层**（见下） |
| **图论** | 几乎全是**隐藏低质 OCR 层**（字体名 `HiddenHorzOCR`，39 万字符，394/412 页含矢量图）。真正的矢量层只剩零散拉丁/数学碎片。 | **全部走 OCR**（dpi=220 + rapidocr） |

> ⚠️ 教训：不能凭"这是矢量 PDF"就假设文字层可用。OSTEP 是矢量排版却带损坏 CMap；图论是矢量图但文字层是隐藏 OCR。**必须逐字体统计字符量 + 抽样比对**后再选路。

### 意外收获：OSTEP 代码块应取文字层而非 OCR

`CourierNew` 层 **100% 干净**：170 页 / 2597 行代码，**0 个损坏 CJK 字符**，缩进、行号全部保留。

```
int flag[2];
int turn;

void init() {
    flag[0] = flag[1] = 0;      // 1->thread wants to grab lock
    turn = 0;                   // whose turn? (thread 0 or 1?)
}
```

OCR 版代码则会出现缺行、缩进丢失、`{` 误识别等问题。**结论：凡是等宽字体行，一律直接取文字层。**

---

## 二、管线（5 步）

```
PDF ──①ocr_pdf──▶ ocr_pages.jsonl ─┐
                    （行+坐标）      │
PDF ──②pdfcode──▶ code_pdf.json    ├─③layout──▶ blocks ─④book2wiki──▶ *.wiki ─⑤import_book──▶ wiki
                    （等宽代码区）   │
     ──structure─▶ struct_*.json ───┘
                    （书签→章/节）
```

| 步骤 | 脚本 | 说明 |
|---|---|---|
| ① OCR | `scripts/ocr_pdf.py` | PyMuPDF 渲染 dpi=220 → rapidocr 多进程池识别 → 按页缓存 JSONL，**断点续跑** |
| ② 代码 | `scripts/pdfcode.py` | 从 `Courier` 字体行抽取代码区，输出 `code_pdf.json`（y 坐标换算到 OCR 像素空间） |
| ③ 版面 | `scripts/layout.py` | OCR 行 → 逻辑块（标题/正文/代码/公式/图注/列表），含段落聚合 |
| ③′ 图形 | `scripts/media.py` | 定位插图/表格/示意图区 → 裁剪 PNG + `figmap.json`（区内 OCR 文本被压制，避免碎片段落） |
| ④ 生成 | `scripts/book2wiki.py` | blocks → wikitext（标题层级、`<syntaxhighlight>`、`<math>`、`[[File:…]]`、分类、导航） |
| ⑤ 导入 | `scripts/import_book.py` `scripts/upload_figs.py` | 经 MediaWiki API（bot 账号）幂等写入页面与插图 |
| 验证 | `scripts/verify_books.py` `scripts/audit.py` `scripts/mk_sheets.py` | 渲染错误 / 红链 / 可疑标题 / 插图 contact sheet |

结构来源：`structure.py` 读 PDF 书签（`doc.get_toc()`）构建 `struct_ostep.json` / `struct_gt.json`
（章/节标题 + 起止页码；本书 TOC 页码偏移 offset=0）。

### 坐标换算（②→③ 对齐的关键）

- OCR 空间：`宽 = 524pt × 220/72 = 1601px`，`高 = 737pt × 220/72 = 2252px`
- `pdfcode.py` 输出的 y 已乘 `scale = dpi/72`，可直接与 `ocr_pages.jsonl` 的坐标比较。

---

## 三、复现步骤

```bash
# 依赖：pymupdf, rapidocr, onnxruntime（隔离 venv）
PY=.../python/envs/default/Scripts/python.exe

cd scripts
PY ocr_pdf.py   <book.pdf> <outdir>/ocr_pages.jsonl --workers 12   # ①OCR（约 0.75s/页）
PY pdfcode.py   <book.pdf> <outdir>/code_pdf.json                  # ②代码（OSTEP 适用）
PY structure.py <book.pdf> struct_<book>.json                      # 结构
PY media.py     <book.pdf> <outdir>/ocr_pages.jsonl <outdir>/img --tag <tag> --mode raster|pile  # ③′插图
PY book2wiki.py ostep|gt                                           # ③④生成 wikitext
PY upload_figs.py both                                             # ⑤上传插图（幂等）
PY import_book.py both                                             # ⑤导入页面
PY verify_books.py && PY audit.py                                  # 验证
# 可选：PY mk_sheets.py both  生成 _verify2/*.png，供 agent 集群视觉终审
```

> `media.py` 的 `--mode`：OSTEP 用 `raster`（内嵌位图），图论用 `pile`（矢量图形 + 图注锚定）。

> 实际运行请用 `.build/book2wiki/` 下的工作副本（路径已写死为绝对路径）。
> `wiki-deploy/book2wiki/scripts/` 是**交付快照**，供阅读与归档。

---

## 四、踩坑记录（重要）

### 1. OCR 并行争抢 → 极慢（ETA 2.4h → 13 分钟）
rapidocr/onnxruntime 默认多线程，多进程 × 多线程严重超订。
**修复**：`os.environ.setdefault("OMP_NUM_THREADS","1")` **必须在 import onnxruntime 之前**设置；多进程池统一 `OMP_NUM_THREADS=1`。另注意清理被强杀残留的 python 进程（`Get-Process python | Stop-Process -Force`）。

### 2. 代码块识别：OSTEP 的短代码块没有行号
最初的 `find_code_regions()` 依赖「左侧连续行号列」，只能识别带行号的块，**短代码块（无行号）全部漏掉**，退化成普通段落。
**修复**：改为从 PDF 的 `CourierNew` 等宽字体层抽取（`pdfcode.py`），彻底不依赖行号启发式。行号启发式保留为无代码层时的回退。

### 3. 中文书笔误："标题被拆成两行"
`第7章` + `进程调度：介绍` 被 OCR 拆成两个标题块。
**修复**：`render_blocks` 开头合并「前一块形如 `第N章`」的连续标题。

### 4. 标题误判（两类，各 77/29 处）
- **图论**：`∑d(v).`、`d(G) :=`、`bij :=` 等**居中显示公式**因字号偏大被当成标题，污染侧栏目录。
- **OSTEP**：`Twrite`、`Reffective`、`EV`、`PFN 200`、`F × Rpeak`、`访问时间 10.195ms`、`续表` 等公式/表格残片。
**修复**：`looks_like_heading()` 严格化——
  ① 中文译本要求**必含汉字**（或匹配 `第N章`/`x.y`/`附录`）；
  ② 数学符号密度 `>0.12` 拒绝；③ 含 `= < > | := → ∈ ⊆ ∑` 拒绝；
  ④ 含「，/ ,」拒绝（真标题用「、」）；⑤ 含 `× ·` 或数值+单位拒绝；
  ⑥ 以虚词（的和所与为在被）结尾的续行拒绝；⑦ 纯 ASCII 短碎片拒绝。
  另新增公式判定 `is_formula()`（符号密度 / 居中 / 编号 / 分段定义行）。
**效果**：两书可疑标题从 106 → **0**。

### 5. 段落跨代码块合并（Peterson 算法处）
正文聚合段落时，空白行被 `strip_header_footer` 后的顺序跳过了代码区，导致代码块**前后的两段正文被拼成一段**（中间夹着代码，读起来错乱）。
**修复**：新增 `crosses_code(y_prev, y_next)`，若相邻两行之间夹着任一 PDF 代码区则不合并。
同时把代码区排除边距从 `+26` 收紧到 `+12`（上边距 `-9`）：OCR 行框紧贴代码区，边距过大会**误吞下一段正文首行**（实测区末 +19px 即下段）。

### 6. 对话体页被误判为目录页（附录C 整页丢失）
`is_toc_line` 原以「含 `…`」为目录标志，但对话体的「……19，实际上……」触发误判，`附录C：关于监器的对话` 整页变空。
**修复**：目录行改为要求**点线后跟页码**（`[.·…]{2,}\s*\d{1,3}$`）或**长点线 ≥4**；目录页判定改为「此类行数 ≥ max(4, 10% 行数)」。
**校准数据**：真目录页 p6–p11 该类行 15–35 行；正文页 0。

### 7. MediaWiki 标题规范化（`/` `:` `_` 不友好）
MediaWiki 把半角 `/` `:` 转成空格、`_` 视为空格，导致**文件名与页面标题不一致、互链失配**。
**修复**：`safe_filename()` 用全角替换：`/→／ :→： \→＼ *→＊ ?→？ "→＂ <→＜ >→＞ |→｜`；所有 landing page 链接同样处理。删除 9 个旧脏页。

### 8. 中文公式用 `<math>\text{...}</math>`
OCR 无法 100% 还原 LaTeX，采用保守策略：纯符号直接放 `<math>`，含中文的包 `\text{}`；
去掉尾部公式编号 `(N.M)` 并渲染为灰色小字。全书 OSTEP 355 处、图论 2159 处 `<math>`。

### 9. Bot 权限与批量删除
bot 组加 `$wgGroupPermissions['bot']['delete']=true` 后会话 rights 仍无 delete。
最终用 maintenance 脚本删除：
```bash
docker exec ogwiki-web sh -c 'MSYS_NO_PATHCONV=1 php maintenance/run.php deleteBatch --u Admin /tmp/dellist.txt'
```
Git Bash 会转换 `/tmp/`，**必须用 `sh -c '...'` 包裹**。

### 10. Bot 密码字符集
MediaWiki 要求 bot 密码匹配 `^[0-9a-w]{32,}$`，否则**不走 bot 登录分支**、报笼统的 `wrongpassword`（即使哈希 verify=true）。当前密码见 `wiki-deploy/botpw.txt`。

---

## 四′、插图 / 表格 / 示意图提取（media.py，本套件最复杂的一环）

### 问题背景
OCR 把**图形内的文字**（状态图标签、时间轴刻度、表格单元）拍平成**竖直单词碎片段落**，
例如「图4.1 加载」被展开成 `CPU / 内存 / 代码 / 静态数据 / 堆 / 栈` 六个竖排段。
正确做法：把图形区域**整体渲染成 PNG 上传**，正文只保留「图注 + `[[File:…]]`」，
并由 `figmap.json` **压制**区内 OCR 文本。

### 两种图形载体的定位方式（因书而异）
| 模式 | 书 | 图形载体 | 定位方式 |
|---|---|---|---|
| `raster` | OSTEP | 内嵌**位图** | `get_image_rects()` 给精确页面位置 |
| `pile` | 图论 | **矢量轮廓**（正文也是矢量字形，无法用 drawings 区分） | 以「图注锚 + 内容带」推定区域 |

`figure_regions()`（图注锚定）**两书共用**：

1. **y 带**（`y_bands`）：把同一视觉行的多个 x 片段合成一带（BAND_GAP=12）。
2. **带分类**：`_band_prose`（正文）、`is_caption_band`（图注）、`_looks_like_code`（代码行）、`_is_toc_page`（目录页整页跳过）。
3. **方向规则**（关键）：**「图 N.M」图注在图形下方 → 向上扩展；「表 N.M」表题在表格上方 → 向下扩展**。
   避免了过去「比较两侧长度取较长者」的误判（会把下方另一张图的代码吞进来）。
4. **横向**：只用该段**自身内容的 x 范围**（`_seg_xext`），防止双栏页跨栏误并。
5. **表格打击**：表题向下的扩展用 `strict=True` 判据——表格数据行常被 OCR 并成长句，
   形似正文；只有「横跨文本栏 + 片段间无列间隙」才算正文，否则继续扩展。

### 本轮的十个坑（按发现顺序）

#### A. 代码清单被裁到只剩末行 `}`
图2.1 `cpu.c`「图注在下方、向上扩展」，但代码行被 `_is_code_band` 判定为边框而提前中断，
结果只抓到末行 `}` + 图注。
**修复**：① 移除扩展循环里的 `_is_code_band` 中断（代码清单本身就是图形内容）；
② 新增 `_looks_like_code`：含 **1-3 位纯数字的窄行号片段**且几乎无中文 → 代码行，**非正文**。

#### B. 无行号代码 / 汇编被当正文
图4.3 `struct proc`、图6.1 汇编 `movl %ebx, 8(%eax)` 无行号、含中文注释，被 `_band_prose` 判为正文。
**修复**：`_looks_like_code` 增加两条——② 含 C 记号且 **CJK 占比 ≤15%**；③ 含代码标点（`%` `#` `;` `{}` …）且 CJK 占比 ≤10%。

#### C. 表格跨行被吞 / 越界吞正文（两头出错）
表15.2 数据行被当正文 → 表格只截到表头；表15.3 又反过来只截半张。
**修复**：表格方向用 `strict` 判据 + `gap`/`span` 双条件（见上「方向规则」节第 5 条）。

#### D. 「图注 ↔ 图形」间距过大（pile 模式）
矢量图形内无 OCR 文字时，图注与图形相距可达 200px+，被 `CONTENT_GAP=120` 挡住 → 近空白图。
**修复**：新增 `CAP_GAP=240`，**仅**「图注 ↔ 首块」这一跳放宽；后续内容带仍用 `CONTENT_GAP`。

#### E. 图注实为正文引用句
『图 10.2.1 就是这样一个图，它拥有顶点 v1,…』被当图注，造出 100px 空图。
**修复**：`CAP_REF` 增补 `就是`；`_bad_caption` 过滤标题样式（`HEAD_LIKE`）与引用句。

#### F. 中文译本用 ASCII 句点断句
图论正文以 `.` / `?` 结尾（而非 `。`），`_band_prose` 的句末判据失效 → 右侧/顶部混入整行正文（BAD_OVER）。
**修复**：句末标点集加入 `. ! ?`；另加「单片段 + 句末标点」判据捕捉短句。

#### G. 目录页被当示意图
**修复**：`_is_toc_page`（点线页码行占比）+ `layout` 侧 TOC 判据。

#### H. 近空白图（只渲染出图注）
**修复**：渲染后算 `_ink_ratio`（非白像素比），`<0.004` 丢弃。

#### I. 图形端到端验证 → 用 **agent 集群**做视觉终审
`mk_sheets.py` 把插图排成 contact sheet（每张 16 图、宽 300px，**控制在 100–400KB** 以免请求体过大 413）。
再派多个 subagent **并行**读 sheet，按 `BAD_CUT / BAD_OVER / BAD_EMPTY / BAD_MIX / BAD_BLEND` 分类返回问题清单，
据此定位根因、逐类修复。教训：单张 sheet 别放太多图/太大（曾 10MB 触发 413）。

#### J. 旧版残留与孤儿图
算法升级后文件名集合变化，磁盘与 wiki 都会留下**孤儿图**（未被 `figmap.json` 引用）。
**修复**：比对 `figmap.json` 引用集合与磁盘/线上文件，清理孤儿；bot 密码需 `delete` grant 才能删文件。
**注意**：MediaWiki 把连续下划线折叠（`bookimg__gt_p017_0` → `Bookimg_gt_p017_0`），
比较文件名时必须先归一化，**否则会把全部引用误报为缺失**。

### 效果
| 指标 | 修复前 | 修复后 |
|---|---|---|
| OSTEP 碎片页（短碎片连续簇 ≥4） | 106 / 500 | **0 / 500** |
| 图论 碎片页 | 56 / 412 | **1 / 412**（仅封面 p0，无意义） |
| OSTEP 插图 | — | 297 张 / 223 页 |
| 图论 插图 | — | 106 张 / 90 页 |
| 页面 `[[File:…]]` 引用 | — | 400 处，**缺失 0** |

> 已知残余（罕见、可接受）：图论 p294（图形上方无文字标签，pile 模式无法定位线条顶边，顶部略切）。

---

## 五、目录结构

```
book2wiki/
├── README.md                 # 本文件
├── struct_ostep.json         # 结构（书签→章/节）
├── struct_gt.json
├── code_pdf.json             # OSTEP 等宽字体代码区
├── scripts/                  # 交付脚本快照
│   ├── ocr_pdf.py            # ①OCR
│   ├── pdfcode.py            # ②PDF 代码抽取
│   ├── structure.py          #   结构
│   ├── layout.py             # ③版面重建
│   ├── book2wiki.py          # ④生成 wikitext
│   ├── media.py              #   插图/表格/示意图提取 → figmap.json + PNG
│   ├── import_book.py        # ⑤导入页面
│   ├── upload_figs.py        #   ⑤导入插图（幂等上传）
│   ├── mk_sheets.py          #   生成 contact sheet（视觉审查用）
│   ├── verify_books.py       #   验证渲染/红链
│   └── audit.py              #   标题审计
├── ostep/pages/              # 59 个 .wiki + pageindex.json（唯一事实来源）
├── ostep/img/                # 297 张插图 + figmap.json
├── graphtheory/pages/        # 19 个 .wiki + pageindex.json
└── graphtheory/img/          # 106 张插图 + figmap.json
```

**修改页面的正确流程**：改对应 `pages/*.wiki` → `verify_books.py` 校验 → `import_book.py` 导入。
（若要改生成逻辑，则改 `scripts/*.py` 后重跑 ③④⑤。）

---

## 六、验收结果

```
页面总数 79  解析成功 79  渲染错误 0  红链 0
OSTEP   files=59  code_blocks=267  math=355  headings=454  可疑标题=0
GT      files=19  code_blocks=1    math=2159 headings=153  可疑标题=0
插图    OSTEP 297 张（223 页）  GT 106 张（90 页）
File 引用 400 处，缺失 0   wiki 图片总数 403
碎片页  OSTEP 0/500   GT 1/412（仅封面）
```

视觉抽检（Playwright + Edge headless，dpi=2）：
- 首页四套知识体系索引正常；
- OSTEP `04抽象：进程`：图4.1/4.2（状态转换图）、表4.1/4.2（进程状态跟踪）均为完整图片 + 图注，**无竖直碎片段落**；
- OSTEP `28锁`：Peterson 算法渲染为带语法高亮的代码框，缩进/注释正确；
- 图论 `第8章无限图`：12 张示意图正常内嵌，无正文混入；
- 图论 `第1章基础知识`：侧栏目录仅含合法小节标题。
