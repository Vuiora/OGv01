"""Append a linked PLDA overview and module pages, preserving existing pages verbatim."""
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import json
import re
import shutil
import xml.etree.ElementTree as ET

from draw_plda_architecture import FILE, MODULE, node, line, text, BLUE, BLACK, RED, GREEN, PINK, PURPLE

DEST = MODULE / "docs/diagrams"
SOURCE_ID = "plda-architecture-dataflow-v1"
OVERVIEW_ID = "plda-modules-overview-v1"
BACKUP = DEST / "TheStructure.before-module-pages-20260915.drawio"

# These are logical responsibilities in the existing implementation; several
# share runtime.py rather than being independently deployed services.
MODULES = [
    dict(key="input", title="输入接口 / 标准化", code="io.py · program_from_dict / load_program", color=BLACK,
         steps=[("请求入口", "JSON version=1；raw / standard / analysis"),
                ("字段与来源检查", "拒绝未知请求字段；确认 source_kind"),
                ("数值载荷适配", "values → Tensor；保留 shape / dtype"),
                ("结构化 IR 装配", "Program / Task / Instruction / Loop")],
         contract="原始入口支持已提供的数值载荷；不会从任意文本或程序自动推导完整语义。",
         failure="版本、字段或载荷非法 → 向调用方返回校验异常；不进入任务队列。"),
    dict(key="validate", title="统一 IR 与契约校验", code="model.py / kernels.py / planner.py · prepare", color=RED,
         steps=[("模型约束", "shape / dtype / 参数范围 / 有效资源描述"),
                ("数据流与单次赋值", "输入先于使用；输出 ID 唯一且存在"),
                ("规格与依赖推导", "算子 infer；数据依赖 + 显式效果约束"),
                ("交付分析输入", "指令、任务、循环契约；已校验张量")],
         contract="依赖安全性以调用方提供的完整效果契约为前提；未知效果按保守顺序处理。",
         failure="非法规格、重复 ID、缺失输入或依赖环 → prepare / analyze 抛出异常。"),
    dict(key="ilp", title="ILP · 指令级分析", code="analysis.py · analyze_ilp / dependencies", color=RED,
         steps=[("指令依赖图", "RAW / WAR / WAW；显式依赖与屏障"),
                ("机器模型约束", "发射宽度 / 单元数量 / 延迟 / 占用周期"),
                ("保守列表调度", "前驱完成且执行端口空闲时发射"),
                ("分析报告", "同周期分组 / 周期数 / IPC / 假设")],
         contract="只分析声明的基本块 IR；不生成机器码，不模拟缓存、分支预测或 ROB。",
         failure="缺失执行单元或依赖环 → 校验异常；未知效果 → 保留原始顺序。"),
    dict(key="tlp", title="TLP · 线程级分析", code="analysis.py · analyze_tlp / dependencies", color=RED,
         steps=[("任务及效果契约", "数据访问范围 / 显式先后关系 / 屏障"),
                ("依赖合并", "写冲突建边；不完整效果保留顺序"),
                ("拓扑层次", "检测环；计算就绪层与拓扑序"),
                ("并行度报告", "Work / Span / Work÷Span / 未知操作对")],
         contract="依赖图交由运行时执行；线程级分析允许一般操作契约，执行器限已支持的张量算子。",
         failure="重复操作 ID、非法依赖、无效成本或依赖环 → 返回校验异常。"),
    dict(key="simd", title="SIMD · 向量分析", code="analysis.py · analyze_simd", color=RED,
         steps=[("循环前提检查", "迭代次数 / 掩码控制 / 归约与浮点重排"),
                ("仿射访问证明", "等步长访问对；检查跨迭代写冲突"),
                ("有限精确检查", "在分析预算内枚举字节访问足迹"),
                ("三值结论", "safe / unsafe / unknown；证据与标量尾部")],
         contract="safe 仅指所给完整契约下的普通循环向量化；运行时使用标量或原生数组后端。",
         failure="未知别名、非仿射访问或超预算 → unknown；发现跨迭代冲突 → unsafe。"),
    dict(key="plan", title="分析报告 / 执行计划", code="planner.py · Plan / prepare", color=RED,
         steps=[("报告汇合", "收集 ILP / TLP / SIMD 分析结果"),
                ("执行图整理", "张量规格表 / 数据依赖 / 拓扑序"),
                ("调度优先级", "反向计算剩余关键路径的估计成本"),
                ("计划交付", "Plan：program / graph / specs / priorities")],
         contract="ILP 与 SIMD 报告随计划提供；当前执行决策直接使用任务 DAG 和优先级。",
         failure="计划构建失败 → 拒绝提交；主机缓冲区总预算在作业开始时再次准入检查。"),
    dict(key="capabilities", title="设备发现 / 能力注册", code="backends.py · discover_devices；runtime.py · capabilities", color=PURPLE,
         steps=[("设备枚举", "CPU；CuPy CUDA；OpenVINO GPU / NPU"),
                ("显式设备身份", "device ID / kind / backend / target"),
                ("能力与配额", "结合适配器能力；槽位 / 逻辑内存预算"),
                ("可用性视图", "设备清单 / 发现诊断 / 隔离设备清单")],
         contract="物理核心及设备内部执行单元由 OS / 厂商运行时分配；PLDA 选择后端与任务配额。",
         failure="驱动或库不可用 → 记录诊断；不注册对应加速器。运行时隔离设备不再参与选择。"),
    dict(key="ready", title="作业队列 / 就绪队列", code="runtime.py · PLDAModule.submit / _run", color=GREEN,
         steps=[("有界作业准入", "max_jobs 信号量；建立 JobHandle"),
                ("FIFO 作业协调", "同一模块一次激活一个作业"),
                ("就绪与跳过检查", "前驱全部成功才就绪；失败后继 SKIPPED"),
                ("任务排序", "按关键路径优先级；就绪任务可并发")],
         contract="控制接口：submit / status / cancel / result；暂时无资源时继续等待。",
         failure="队列满 → 上游背压；取消或作业超时 → 停止活跃任务并汇总终态。"),
    dict(key="placement", title="候选设备 / 成本选择", code="planner.py · eligibility / placement_cost_ms；runtime.py · _run", color=GREEN,
         steps=[("能力及语义过滤", "设备类型 / 算子 / dtype；strict 只用 CPU"),
                ("可用性过滤", "排除隔离与本任务已失败设备；检查预算"),
                ("成本估计", "启动 + 计算 + 数据搬运 + 忙设备等待"),
                ("选择或等待", "选择估计最优设备；忙碌则等待资源")],
         contract="估计成本依赖配置和测量；可并行不等于有收益，最终设备支持还由驱动校验。",
         failure="无合格设备 → NO_ELIGIBLE_DEVICE；仅槽位或剩余预算不足时继续等待。"),
    dict(key="ledger", title="资源配额账本", code="planner.py · reservation_bytes；runtime.py · slots / memory / release", color=GREEN,
         steps=[("估算预留量", "2 ×（唯一输入字节 + 输出字节）+ workspace"),
                ("配额检查", "使用槽位 < slots；总预留不超过 memory_bytes"),
                ("启动前预留", "槽位 +1；内存记账；生成 RESERVED 事件"),
                ("退出后回收", "确认进程已停止；扣减账本；RELEASE 事件")],
         contract="这是逻辑准入预算，不能强制限制 OS RSS 或驱动工作区的真实峰值。",
         failure="进程仍存活时禁止回收；监督器无法确认退出 → 隔离设备，阻止后续复用。"),
    dict(key="buffers", title="主机不可变数据区", code="model.py · Tensor / TensorSpec；runtime.py · store", color=BLACK,
         steps=[("作业输入快照", "analyze 深拷贝 Program；保护提交后语义"),
                ("不可变张量存储", "TensorID → bytes + TensorSpec"),
                ("只读输入引用", "按任务 inputs 取数；按需复制到工作进程"),
                ("成功结果入库", "校验后的输出一次写入；供后继读取")],
         contract="主机保存输入和已提交输出；当前没有跨任务持久设备缓存或零拷贝数据通路。",
         failure="主机预算超限 → HOST_BUDGET_EXCEEDED；未校验及失败结果不写入 store。"),
    dict(key="staging", title="任务与数据分发", code="sharding.py · shard_elementwise；runtime.py · Process；backends.py · worker", color=GREEN,
         steps=[("可选分片预处理", "等规格一维逐元素输入 → 分片任务 + concat"),
                ("任务输入装配", "读取任务 Tensor 引用；绑定已选设备"),
                ("隔离执行尝试", "spawn 工作进程；每次尝试独立输出目录"),
                ("后端数据交付", "CPU / GPU / NPU 适配器；加速器搬运输入")],
         contract="分片由提交前辅助函数构造；concat 是 CPU 任务。每个分片仍遵守同一调度与校验流程。",
         failure="进程启动失败 → WORKER_CRASH；停止并回收已启动进程后，按重试策略处理。"),
    dict(key="cpu-backend", title="CPU 后端", code="backends.py · execute / worker；kernels.py", color=GREEN,
         steps=[("任务与输入接收", "隔离工作进程；设置本地库线程数为 1"),
                ("数值模式选择", "strict → 标量参考；relaxed + NumPy → 原生数组"),
                ("CPU 算子执行", "计算输出；转换为预期 shape / dtype"),
                ("完成结果发布", "写 output.bin；最后原子发布 status.json")],
         contract="系统调度 CPU 核心；NumPy 的向量实现由库决定，ILP / SIMD 报告不直接生成代码。",
         failure="内核、类型或内存错误 → worker 错误状态；崩溃或超时由监督器捕获。"),
    dict(key="gpu-backend", title="GPU 后端", code="backends.py · execute / _openvino_execute", color=GREEN,
         steps=[("显式设备绑定", "CUDA device index 或 OpenVINO Intel GPU target"),
                ("主机到设备", "CuPy cp.asarray；OpenVINO 输入与模型编译"),
                ("GPU 执行", "CuPy 数组算子 或 OpenVINO infer request"),
                ("同步与回传", "CuPy 回主机并同步；规格转换后发布状态")],
         contract="加速器要求 relaxed 数值模式；每个任务独立进程，启动、编译和搬运成本计入总时间。",
         failure="后端不可用、OOM、内核失败 → 错误事件；崩溃 / 超时可能触发设备隔离。"),
    dict(key="npu-backend", title="NPU 后端", code="backends.py · _openvino_execute / worker", color=GREEN,
         steps=[("目标及输入检查", "显式 NPU target；非空 float32 张量"),
                ("算子图构建", "建立 OpenVINO 模型；编译到指定 NPU"),
                ("推理请求", "绑定主机输入；同步 infer"),
                ("输出回传", "转换为 Tensor；写数据并发布完成状态")],
         contract="显式目标避免 AUTO 静默改到 CPU；当前每次任务重新编译，常驻模型缓存尚未接入。",
         failure="设备或算子不支持、编译或推理异常 → 上报；按有限重试及隔离规则处理。"),
    dict(key="result-gate", title="完成事件 / 结果校验", code="runtime.py · _run（进程回收与结果读取）", color=PINK,
         steps=[("完成观察", "检查工作进程退出状态；区分超时与崩溃"),
                ("回收与状态读取", "确认退出后释放资源；读取 status.json"),
                ("结果完整性", "完成期限 / output.bin 字节数 / Tensor 规格"),
                ("成功与失败分流", "成功交付提交；错误交付生命周期监督")],
         contract="后端先校验 shape 并转换 dtype；运行时检查预期字节数。此处不逐项重算数值结果。",
         failure="缺失元数据、格式错误或输出长度错误 → INVALID_RESULT；未校验结果不提交。"),
    dict(key="supervisor", title="异常与生命周期监督", code="runtime.py · _stop / fail / _run_guarded / JobHandle", color=PURPLE,
         steps=[("生命周期监测", "任务 / 作业超时；取消；崩溃；后端错误"),
                ("停止与退出确认", "terminate → join → 必要时 kill；再回收配额"),
                ("隔离与重试决策", "排除失败设备；可重试错误受 max_retries 限制"),
                ("任务与作业收尾", "重试 PENDING；否则 FAILED；依赖后继 SKIPPED")],
         contract="只重试内置纯张量算子；作业期限从开始运行计时，调用 result 的等待超时不取消作业。",
         failure="无法确认工作进程停止或监督器自身异常 → SUPERVISOR_ERROR，并隔离整个设备清单。"),
    dict(key="commit", title="结果提交 / 输出接口", code="runtime.py · store / JobResult / JobHandle；io.py", color=PINK,
         steps=[("一次提交", "已校验 Tensor 写入不可变 store"),
                ("成功状态发布", "任务 SUCCEEDED；记录 COMMITTED 事件"),
                ("后继与输出", "下轮扫描唤醒就绪后继；收集请求的 outputs"),
                ("对外返回", "JobResult：输出 / 终态 / 错误 / 尝试数 / 事件")],
         contract="失败作业仍可返回已提交的可用输出；下游需同时检查作业状态与各任务状态。",
         failure="失败、取消或超时状态经 JobResult 返回；未完成任务的数据不会被当作成功结果发布。"),
]
for i, module in enumerate(MODULES, 1):
    module.update(number=f"M{i:02}", page_id=f"plda-module-{module['key']}-v1", page_index=i + 3)
BY_KEY = {m["key"]: m for m in MODULES}


def linked(root, cell, page_id):
    """Native draw.io page link, retained by its mxGraph codec."""
    index = list(root).index(cell)
    wrapper = ET.Element("object", id=cell.attrib.pop("id"), label=cell.attrib.pop("value", ""),
                         link=f"data:page/id,{page_id}")
    root.remove(cell)
    wrapper.append(cell)
    root.insert(index, wrapper)
    return wrapper


def straight(root, ident, source, target, color, **kwargs):
    cell = line(root, ident, source, target, color, **kwargs)
    cell.set("style", cell.get("style").replace("edgeStyle=orthogonalEdgeStyle;", "edgeStyle=none;noEdgeStyle=1;"))
    return cell


def legend(root, y, width=1460):
    text(root, "legend-title", "数据线图例", 30, y, 180, 28, 16, bold=True)
    for i, (color, label, dashed) in enumerate([
        (BLUE, "原始数据", False), (BLACK, "标准数据 / 张量", False), (RED, "分析数据 / 报告", False),
        (GREEN, "执行任务 / 设备数据", False), (PINK, "结果回传", False), (PURPLE, "控制 / 状态 / 异常", True),
    ]):
        x, yy = 30 + (i % 3) * (width // 3), y + 52 + (i // 3) * 34
        a, b = f"key-a-{i}", f"key-b-{i}"
        node(root, a, "", x, yy, 1, 1, "none", "none", "opacity=0;")
        node(root, b, "", x + 65, yy, 1, 1, "none", "none", "opacity=0;")
        straight(root, f"key-edge-{i}", a, b, color, exit=(1, .5), entry=(0, .5), dashed=dashed)
        text(root, f"key-label-{i}", label, x + 80, yy - 14, 350, 28, 15)


def overview(source):
    page = deepcopy(source)
    page.set("id", OVERVIEW_ID)
    page.set("name", "PLDA 模块全景总览")
    model = page.find("mxGraphModel")
    model.set("pageWidth", "2300")
    model.set("pageHeight", "1840")
    root = model.find("root")
    cells = {c.get("id"): c for c in root}
    cells["detail-title"].set("value", "PLDA 模块全景总览 · M01–M18")
    cells["detail-subtitle"].set("value", "同页展示全部 18 个功能模块与数据线；模块节点及底部索引可跳转到独立分页。第 2 页原图保持不变。")
    cells["detail-footer"].set("value", "分页导航 · 编号与模块节点一一对应；各分页保留上游、下游与数据线颜色")
    for m in MODULES:
        cell = cells[m["key"]]
        value = cell.get("value")
        if value.startswith("<b>"):
            value = value.replace("<b>", f"<b>{m['number']} · ", 1)
        else:
            value = f"<b>{m['number']}</b> · {value}"
        cell.set("value", value)
        cell.set("style", cell.get("style") + "fontSize=15;")
        linked(root, cell, m["page_id"])
    for i, m in enumerate(MODULES):
        x, y = 30 + (i % 6) * 374, 1560 + (i // 6) * 78
        cell = node(root, f"index-{m['key']}", f"<b>{m['number']} · {m['title']}</b><br>第 {m['page_index']} 页 ↗",
                    x, y, 354, 64, "#ffffff", "#d6b656", "fontSize=14;spacing=5;")
        linked(root, cell, m["page_id"])
    return page


def edge_color(edge):
    return re.search(r"(?:^|;)strokeColor=([^;]+)", edge.get("style")).group(1)


def interface_label(key, original):
    if key in BY_KEY:
        m = BY_KEY[key]
        return f"<b>{m['number']} · {m['title']}</b>"
    label = re.sub(r"(?:<br\s*/?>\s*){2,}", "<br>", original[key].get("value", key))
    return "<b>外部接口</b><br>" + label


def flow_name(edge):
    if edge.get("value"):
        return edge.get("value")
    return {BLUE: "原始数值载荷", BLACK: "标准数据 / 张量", RED: "IR / 分析报告",
            GREEN: "执行任务 / 设备数据", PINK: "结果 / 完成事件", PURPLE: "控制 / 状态"}[edge_color(edge)]


def module_page(m, original, edges):
    page = ET.Element("diagram", id=m["page_id"], name=f"{m['number']} {m['title']}")
    model = ET.SubElement(page, "mxGraphModel", dx="1500", dy="1200", grid="1", gridSize="10", guides="1",
                          tooltips="1", connect="1", arrows="1", fold="1", page="1", pageScale="1",
                          pageWidth="1520", pageHeight="1210", math="0", shadow="0")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")
    text(root, "title", f"{m['number']} · {m['title']}", 30, 24, 1150, 50, 28, bold=True)
    back = node(root, "back", "↗ 返回模块全景总览", 1210, 30, 280, 44, "#dae8fc", "#6c8ebf", "fontSize=16;")
    linked(root, back, OVERVIEW_ID)
    text(root, "code", "实现对应：plda / " + m["code"], 30, 98, 1450, 36, 16, "#555555")
    text(root, "input-heading", "输入 / 上游模块", 1190, 180, 300, 32, 20, bold=True)
    text(root, "output-heading", "输出 / 下游模块", 30, 180, 300, 32, 20, bold=True)
    boundary = node(root, "module", f"<b>{m['number']} · {m['title']}</b>", 510, 175, 540, 660,
                    "#fff8df", "#d6b656", "verticalAlign=top;spacingTop=22;fontSize=22;strokeWidth=2;")
    for i, (heading, desc) in enumerate(m["steps"]):
        y = 95 + i * 104
        node(root, f"step-{i}", f"<b>{i + 1:02} · {heading}</b><br>{desc}", 35, y, 470, 76,
             "#ffffff", "#d6b656", "fontSize=16;spacing=7;", parent="module")
        if i:
            straight(root, f"step-edge-{i}", f"step-{i-1}", f"step-{i}", m["color"], exit=(.5, 1), entry=(.5, 0))
    node(root, "contract", "<b>边界 / 约定</b><br>" + m["contract"], 35, 530, 470, 100,
         "#f5f5f5", "#bbbbbb", "fontSize=15;spacing=9;", parent="module")

    incoming = [e for e in edges if e.get("target") == m["key"]]
    outgoing = [e for e in edges if e.get("source") == m["key"]]
    for kind, links in (("in", incoming), ("out", outgoing)):
        count = len(links)
        for i, edge in enumerate(links):
            other = edge.get("source" if kind == "in" else "target")
            x = 1190 if kind == "in" else 30
            y = 430 if count == 1 else 255 + i * (450 / (count - 1))
            color = edge_color(edge)
            ident = f"{kind}-{i}"
            # Separate result and dispatch ports remain distinguishable even when
            # the same peer appears on both sides of a backend page.
            cell = node(root, ident, interface_label(other, original) + "<br>" + flow_name(edge),
                        x, y, 300, 106, "#ffffff", color, "fontSize=16;spacing=8;")
            if other in BY_KEY:
                linked(root, cell, BY_KEY[other]["page_id"])
            fraction = (y + 53 - 175) / 660
            source, target = (ident, "module") if kind == "in" else ("module", ident)
            exit_anchor = (0, .5) if kind == "in" else (0, fraction)
            entry_anchor = (1, fraction) if kind == "in" else (1, .5)
            line(root, f"{kind}-edge-{i}", source, target, color,
                 exit=exit_anchor, entry=entry_anchor, dashed="dashed=1" in edge.get("style"))
    node(root, "exception", "<b>异常 / 保守处理路径</b><br>" + m["failure"], 510, 890, 540, 108,
         "#e1d5e7", "#9673a6", "fontSize=16;spacing=10;")
    line(root, "exception-edge", "module", "exception", PURPLE, exit=(.5, 1), entry=(.5, 0), dashed=True)
    text(root, "scope", "图示为逻辑职责展开；多个职责共用同一代码文件。输入从右进入，输出向左交付；点击相邻模块可继续跳转。",
         30, 1030, 1460, 32, 15, "#666666")
    legend(root, 1080)
    return page


def page_segments(blob):
    return re.findall(rb"<diagram\b[^>]*>.*?</diagram>", blob, flags=re.DOTALL)


def validate(blob, preserved):
    document = ET.fromstring(blob)
    pages = document.findall("diagram")
    page_ids = {p.get("id") for p in pages}
    assert len(page_ids) == len(pages), "duplicate page id"
    segments = page_segments(blob)
    assert segments[:2] == preserved, "original first or second page changed"
    all_links = 0
    for page in pages:
        root = page.find("mxGraphModel/root")
        ids = [cell.get("id") for cell in root]
        assert None not in ids and len(ids) == len(set(ids)), "duplicate or missing cell id"
        for item in root:
            cell = item if item.tag == "mxCell" else item.find("mxCell")
            assert cell is not None
            for attr in ("parent", "source", "target"):
                assert cell.get(attr) is None or cell.get(attr) in ids, f"dangling {attr}"
            link = item.get("link", "")
            if link:
                assert link.startswith("data:page/id,") and link.removeprefix("data:page/id,") in page_ids
                all_links += 1
    assert all(m["page_id"] in page_ids for m in MODULES)
    return len(pages), all_links


def main():
    before = FILE.read_bytes()
    original_pages = page_segments(before)[:2]
    mxfile = ET.fromstring(before)
    source = next(p for p in mxfile.findall("diagram") if p.get("id") == SOURCE_ID)
    original = {c.get("id"): c for c in source.find("mxGraphModel/root")}
    edges = [c for c in original.values() if c.get("edge") == "1" and not c.get("id").startswith("legend-")]
    # Flatten only the cosmetic result junction in module detail interfaces.
    junction_inputs = [e.get("source") for e in edges if e.get("target") == "return-junction"]
    detail_edges = [e for e in edges if "return-junction" not in (e.get("source"), e.get("target"))]
    template = next(e for e in edges if e.get("source") == "return-junction")
    for key in junction_inputs:
        edge = deepcopy(template)
        edge.set("source", key)
        detail_edges.append(edge)
    new_pages = [overview(source)] + [module_page(m, original, detail_edges) for m in MODULES]
    generated_ids = {p.get("id") for p in new_pages}
    # Only replace our generated pages. Do not reserialize the user's two pages.
    for segment in page_segments(before):
        if ET.fromstring(segment).get("id") in generated_ids:
            before = before.replace(segment, b"", 1)
    payload = []
    for page in new_pages:
        ET.indent(page, space="  ")
        payload.append(ET.tostring(page, encoding="utf-8"))
    prefix, closing, suffix = before.rpartition(b"</mxfile>")
    assert closing, "missing mxfile closing tag"
    after = prefix.rstrip() + b"\n" + b"\n".join(payload) + b"\n</mxfile>" + suffix
    count, link_count = validate(after, original_pages)
    DEST.mkdir(parents=True, exist_ok=True)
    if not BACKUP.exists():
        shutil.copy2(FILE, BACKUP)
    temporary = FILE.with_suffix(".drawio.tmp")
    temporary.write_bytes(after)
    temporary.replace(FILE)
    manifest = {
        "file": str(FILE), "page_count": count, "module_count": len(MODULES), "native_page_links": link_count,
        "preserved_pages": [{"index": i + 1, "sha256": sha256(p).hexdigest()} for i, p in enumerate(original_pages)],
        "overview": {"index": 3, "id": OVERVIEW_ID, "preview": "plda-modules-overview.png"},
        "modules": [{k: m[k] for k in ("number", "key", "title", "page_id", "page_index", "code")} for m in MODULES],
    }
    (DEST / "module-pages.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"pages": count, "modules": len(MODULES), "page_links": link_count,
                      "first_two_pages_byte_identical": True, "file": str(FILE)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
