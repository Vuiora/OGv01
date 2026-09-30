"""Expand PLDA in the existing draw.io file and add an editable detailed page.

Run from the project or module directory. Uses native mxGraph XML, not a raster overlay.
"""
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

MODULE = Path(__file__).resolve().parents[1]
PROJECT = MODULE.parent
FILE = PROJECT / "TheStructure.drawio"
BACKUP = MODULE / "docs/diagrams/TheStructure.before-plda-20260915.drawio"
PAGE_ID = "plda-architecture-dataflow-v1"
PREFIX = "8rw-NWJHrkYK3E8QMIOX-"

BLUE, BLACK, RED, GREEN, PINK, PURPLE = "#0000FF", "#202020", "#FF0000", "#005700", "#FF0080", "#7B1FA2"
BASE = "rounded=0;whiteSpace=wrap;html=1;fontFamily=Microsoft YaHei;fontSize=16;spacing=9;"


def node(root, ident, text, x, y, w, h, fill="#fff2cc", stroke="#d6b656", extra="", parent="1"):
    cell = ET.SubElement(root, "mxCell", id=ident, value=text,
                         style=BASE + f"fillColor={fill};strokeColor={stroke};" + extra,
                         vertex="1", parent=parent)
    ET.SubElement(cell, "mxGeometry", x=str(x), y=str(y), width=str(w), height=str(h), **{"as": "geometry"})
    return cell


def line(root, ident, source, target, color=BLACK, label="", points=(),
         exit=(0, .5), entry=(1, .5), dashed=False, width=2, label_pos=0):
    style = (f"edgeStyle=orthogonalEdgeStyle;rounded=0;orthogonalLoop=1;jettySize=auto;html=1;"
             f"strokeColor={color};strokeWidth={width};endArrow=block;endFill=1;jumpStyle=arc;jumpSize=7;"
             f"fontFamily=Microsoft YaHei;fontSize=13;fontColor={color};labelBackgroundColor=#FFFFFF;"
             f"exitX={exit[0]};exitY={exit[1]};exitDx=0;exitDy=0;"
             f"entryX={entry[0]};entryY={entry[1]};entryDx=0;entryDy=0;")
    if dashed:
        style += "dashed=1;dashPattern=6 4;"
    cell = ET.SubElement(root, "mxCell", id=ident, value=label, edge="1", parent="1",
                         source=source, target=target, style=style)
    geom = ET.SubElement(cell, "mxGeometry", relative="1", x=str(label_pos), **{"as": "geometry"})
    if points:
        array = ET.SubElement(geom, "Array", **{"as": "points"})
        for x, y in points:
            ET.SubElement(array, "mxPoint", x=str(x), y=str(y))
    return cell


def text(root, ident, value, x, y, w, h, size=16, color="#202020", bold=False):
    return node(root, ident, value, x, y, w, h, "none", "none",
                f"align=left;verticalAlign=middle;spacing=0;fontSize={size};fontColor={color};fontStyle={1 if bold else 0};")


def main():
    BACKUP.parent.mkdir(parents=True, exist_ok=True)
    if not BACKUP.exists():
        shutil.copy2(FILE, BACKUP)
    tree = ET.parse(FILE)
    mxfile = tree.getroot()
    overview = mxfile.find("diagram/mxGraphModel/root")
    # Idempotent additions; preserve every original vertex and edge id.
    for cell in list(overview):
        if cell.get("id", "").startswith("plda-overview-"):
            overview.remove(cell)
    cells = {c.get("id"): c for c in overview}
    plda = cells[PREFIX + "83"]
    plda.set("value", "PLDA")
    plda.set("style", BASE + "fillColor=#ffcd28;gradientColor=#ffa500;strokeColor=#d79b00;"
             "align=center;verticalAlign=top;fontSize=19;fontStyle=1;spacingTop=10;")
    stages = [("input", "输入接口<br>标准化 / 校验", 42), ("analysis", "并行分析<br>ILP · TLP · SIMD", 110),
              ("schedule", "任务计划<br>资源分配", 178), ("dispatch", "数据分发<br>结果回传", 246)]
    for name, label, y in stages:
        node(overview, f"plda-overview-{name}", label, 10, y, 100, 48,
             extra="fontSize=11;spacing=3;", parent=PREFIX + "83")
    for index in range(3):
        line(overview, f"plda-overview-flow-{index}", f"plda-overview-{stages[index][0]}",
             f"plda-overview-{stages[index+1][0]}", RED if index < 2 else GREEN,
             exit=(.5, 1), entry=(.5, 0), width=1)
    node(overview, "plda-overview-page-note", "详见第 2 页", 10, 299, 100, 16,
         "none", "none", "fontSize=9;spacing=0;", parent=PREFIX + "83")
    # Existing upstream links show the origin unambiguously: 35 -> 45 is analysis,
    # while 12 -> 46 is standard data. Correct only these swapped labels.
    cells[PREFIX + "45"].set("value", "符合接口的结构化标准分析数据")
    cells[PREFIX + "46"].set("value", "符合接口的结构化标准数据")
    for ident, source, color, route_x, source_y, target_y, port in [
        ("57", "46", BLACK, -210, 385, 302, .21),
        ("56", "45", RED, -225, 445, 316, .50),
        ("47", "44", BLUE, -240, 525, 330, .79),
    ]:
        old = cells[PREFIX + ident]
        overview.remove(old)
        line(overview, PREFIX + ident, PREFIX + source, "plda-overview-input", color,
             points=[(route_x, source_y), (route_x, target_y)], entry=(1, port), width=1)
    for index, (ident, target, lane, target_y) in enumerate([
        ("79", "53", -550, 185), ("80", "62", -530, 485),
        ("81", "66", -510, 775), ("82", "70", -490, 1175),
    ]):
        overview.remove(cells[PREFIX + ident])
        port = [.15, .38, .62, .85][index]
        line(overview, PREFIX + ident, "plda-overview-dispatch", PREFIX + target, GREEN,
             points=[(lane, 496 + 48 * port), (lane, target_y)], exit=(0, port), width=1)
    node(overview, "plda-overview-results", "已提交的执行结果<br>返回路由 / 快速数据通道", -140, 650, 275, 65,
         "#f8cecc", "#b85450", "fontSize=13;")
    line(overview, "plda-overview-result-out", "plda-overview-dispatch", "plda-overview-results", PINK,
         points=[(-250, 534), (-250, 682.5)], exit=(1, .8), entry=(0, .5), width=1)
    line(overview, "plda-overview-result-channel", "plda-overview-results", PREFIX + "19", PINK,
         "结果回传", points=[(-2.5, 750), (405, 750)], exit=(.5, 1), entry=(.5, 1), dashed=True, width=1)
    for page in list(mxfile.findall("diagram")):
        if page.get("id") == PAGE_ID:
            mxfile.remove(page)
    page = ET.SubElement(mxfile, "diagram", id=PAGE_ID, name="PLDA 架构与数据流")
    model = ET.SubElement(page, "mxGraphModel", dx="2300", dy="1540", grid="1", gridSize="10",
                          guides="1", tooltips="1", connect="1", arrows="1", fold="1", page="1",
                          pageScale="1", pageWidth="2300", pageHeight="1540", math="0", shadow="0")
    root = ET.SubElement(model, "root")
    ET.SubElement(root, "mxCell", id="0")
    ET.SubElement(root, "mxCell", id="1", parent="0")
    text(root, "detail-title", "PLDA 架构与数据流", 30, 22, 1500, 42, 30, bold=True)
    text(root, "detail-subtitle", "沿用总图：输入由右向左进入处理单元；结果由左向右回传。所有节点与连线均可编辑。",
         30, 80, 2240, 30, 17)
    node(root, "communication", "通信基础设施", 390, 140, 1880, 1190, "#dae8fc", "#6c8ebf",
         "align=left;verticalAlign=top;fontStyle=1;fontSize=20;")
    node(root, "load-balance", "负载均衡模块", 430, 180, 1400, 1110, "#d5e8d4", "#82b366",
         "align=left;verticalAlign=top;fontStyle=1;fontSize=19;")
    node(root, "plda-boundary", "PLDA  |  Parallel Logic Determination Algorithm", 450, 230, 1360, 1030,
         "#fff2cc", "#d79b00", "align=left;verticalAlign=top;fontSize=22;fontStyle=1;strokeWidth=2;")
    text(root, "implementation-note", "当前实现：ILP / SIMD 输出分析报告；TLP 驱动任务调度。常驻进程与模型缓存尚未接入。",
         475, 273, 1290, 18, 12, "#745500")
    node(root, "pool", "DEVICE / 处理单元池", 30, 230, 300, 1030, "#008a00", "#005700",
         "align=left;verticalAlign=top;fontColor=#ffffff;fontStyle=1;fontSize=21;")
    node(root, "inventory", "设备清单 / 能力信息<br><br>CPU · GPU · NPU<br>内存预算 / 可用状态", 60, 330, 250, 130,
         "#647687", "#314354", "fontColor=#ffffff;")
    node(root, "hardware-note", "核心由 OS / 厂商驱动管理<br><br>CPU：进程 → CPU 核<br>GPU：后端 → SM / EU<br>NPU：编译图 → 计算单元<br><br>槽位表示并发任务配额", 60, 520, 250, 235,
         "#647687", "#314354", "fontColor=#ffffff;fontSize=15;")
    node(root, "router", "路由设备  |  承接总图输入", 1870, 230, 360, 1030, "#d5e8d4", "#82b366",
         "align=left;verticalAlign=top;fontSize=19;fontStyle=1;")
    for ident, label, y in [("raw", "原始数据<br>数值载荷 / 来源描述", 300),
                             ("standard", "结构化标准数据<br>张量 / 形状 / dtype", 470),
                             ("analysis-input", "结构化标准分析数据<br>任务图 / 指令 / 循环契约", 640)]:
        node(root, ident, label, 1910, y, 290, 100, "#f8cecc", "#b85450")
    node(root, "control-input", "调度方控制<br>取消 / 作业期限 / 状态查询", 1910, 850, 290, 100,
         "#e1d5e7", "#9673a6")
    node(root, "output", "已提交结果 / 作业状态<br>返回路由与快速数据通道", 1910, 1110, 290, 110,
         "#f8cecc", "#b85450")
    node(root, "input", "输入接口 / 标准化<br>版本化请求 · 原始数值适配<br>Program / Task / Tensor", 1540, 300, 230, 110)
    node(root, "validate", "统一 IR 与契约校验<br>形状 / 类型 / SSA<br>访问范围 / 显式依赖", 1540, 470, 230, 100)
    node(root, "ilp", "ILP · 指令级分析<br>RAW / WAR / WAW<br>发射宽度 / 执行单元", 1260, 300, 220, 100)
    node(root, "tlp", "TLP · 线程级分析<br>DAG / Work / Span<br>拓扑序 / 关键路径", 1260, 440, 220, 100)
    node(root, "simd", "SIMD · 向量分析<br>仿射访问 / 迭代冲突<br>归约约束 / 尾部", 1260, 580, 220, 100)
    node(root, "plan", "分析报告 / 执行计划<br><br>依赖与顺序 / 未知原因<br>DAG + 调度优先级", 970, 435, 230, 130)
    node(root, "capabilities", "设备发现 / 能力注册<br><br>算子 · dtype · 设备种类<br>槽位 / 内存 / 健康状态", 690, 300, 230, 130)
    node(root, "ready", "作业队列 / 就绪队列<br>有界 FIFO · 前驱完成检查<br>按关键路径优先级选择", 970, 650, 230, 100)
    node(root, "placement", "候选设备 / 成本选择<br>能力与数值语义过滤<br>启动 + 计算 + 搬运成本", 690, 650, 230, 100)
    node(root, "ledger", "资源配额账本<br>槽位 / 内存预留与释放<br>资源不足时等待", 690, 790, 230, 95)
    node(root, "buffers", "主机不可变数据区<br>输入 / 已提交输出<br>Tensor ID · shape · dtype", 1510, 800, 260, 115,
         "#f8cecc", "#b85450")
    node(root, "staging", "任务与数据分发<br>逐元素分片 / 有序拼接<br>输入暂存 / 主机 → 设备", 1010, 840, 230, 100)
    for ident, label, core, corelabel, y in [
        ("cpu-backend", "CPU 后端<br>独立进程 · 标量 / NumPy", "cpu-core", "CPU CORE_1 … CORE_X", 900),
        ("gpu-backend", "GPU 后端<br>CuPy / OpenVINO", "gpu-core", "GPU SM / EU / 计算队列", 1015),
        ("npu-backend", "NPU 后端<br>OpenVINO · 显式目标", "npu-core", "NPU 计算单元 / 推理请求", 1130),
    ]:
        node(root, ident, label, 470, y, 230, 75)
        node(root, core, corelabel, 60, y, 250, 75, "#647687", "#314354", "fontColor=#ffffff;")
    node(root, "result-gate", "完成事件 / 结果校验<br>输出规格 / 字节数<br>成功与失败分流", 1010, 1125, 230, 95)
    node(root, "supervisor", "异常与生命周期监督<br>超时 / 取消 / 崩溃 / OOM<br>退出确认 → 回收 / 隔离<br>有限重试 · 跳过后继", 1260, 990, 230, 115,
         "#e1d5e7", "#9673a6", "fontSize=15;")
    node(root, "commit", "结果提交 / 输出接口<br>验证后提交不可变输出<br>唤醒后继 · 汇总状态", 1510, 1110, 260, 110,
         "#f8cecc", "#b85450")
    node(root, "return-junction", "", 870, 1240, 10, 10, PINK, PINK, "ellipse;aspect=fixed;spacing=0;")
    # Input contract and data lanes follow the colors of the original drawing.
    for ident, color, sx, sy, port in [("raw", BLUE, 1850, 350, .22),
                                       ("standard", BLACK, 1835, 520, .5),
                                       ("analysis-input", RED, 1820, 690, .78)]:
        line(root, f"edge-{ident}", ident, "input", color,
             points=[(sx, sy), (sx, 300 + 110 * port)], entry=(1, port))
    line(root, "edge-input-validate", "input", "validate", BLACK, "标准化请求",
         exit=(.5, 1), entry=(.5, 0))
    for index, ident in enumerate(("ilp", "tlp", "simd")):
        source_y = 485 + index * 32
        target_y = [350, 490, 630][index]
        lane = [1500, 1510, 1520][index]
        line(root, f"edge-ir-{ident}", "validate", ident, RED,
             points=[(lane, source_y), (lane, target_y)], exit=(0, (source_y-470)/100))
        line(root, f"edge-{ident}-report", ident, "plan", RED,
             points=[(1230 - index * 8, target_y), (1230 - index * 8, [465, 500, 540][index])],
             entry=(1, ([465, 500, 540][index] - 435) / 130))
    line(root, "edge-plan-ready", "plan", "ready", RED, "DAG / 优先级", exit=(.5, 1), entry=(.5, 0))
    line(root, "edge-ready-placement", "ready", "placement", GREEN)
    line(root, "edge-placement-ledger", "placement", "ledger", GREEN, exit=(.5, 1), entry=(.5, 0))
    line(root, "edge-inventory", "inventory", "capabilities", PURPLE, "设备能力 / 状态", dashed=True,
         exit=(1, .3), entry=(0, .5), points=[(360, 369), (360, 365)])
    line(root, "edge-capabilities-placement", "capabilities", "placement", PURPLE, "可用设备清单",
         exit=(.5, 1), entry=(.5, 0), dashed=True)
    line(root, "edge-data-buffer", "validate", "buffers", BLACK, "校验后的张量",
         exit=(.5, 1), entry=(.558, 0))
    line(root, "edge-buffer-stage", "buffers", "staging", BLACK, "只读输入 / 分片",
         points=[(1430, 857.5), (1430, 890)])
    line(root, "edge-ledger-stage", "ledger", "staging", GREEN, "分配许可",
         exit=(1, .5), entry=(0, .25), points=[(965, 837.5), (965, 865)])
    for index, (backend, core, y) in enumerate((
        ("cpu-backend", "cpu-core", 937.5), ("gpu-backend", "gpu-core", 1052.5),
        ("npu-backend", "npu-core", 1167.5))):
        stage_y = [905, 920, 935][index]
        lane = [780, 805, 830][index]
        line(root, f"edge-stage-{backend}", "staging", backend, GREEN,
             points=[(lane, stage_y), (lane, y)], exit=(0, (stage_y-840)/100))
        line(root, f"edge-{backend}-{core}", backend, core, GREEN, "任务 / 数据", width=2)
        line(root, f"edge-{core}-return", core, backend, PINK, exit=(1, .74), entry=(0, .74), width=1.5)
        return_lane = 730 + index * 20
        line(root, f"edge-{backend}-complete", backend, "return-junction", PINK,
             points=[(return_lane, y + 18), (return_lane, 1245)],
             exit=(1, .74), entry=(0, .5), width=1.5)
    line(root, "edge-results-gate", "return-junction", "result-gate", PINK, "结果 / 完成事件",
         points=[(1125, 1245)], exit=(1, .5), entry=(.5, 1))
    line(root, "edge-result-commit", "result-gate", "commit", PINK, "成功结果",
         exit=(1, .75), entry=(0, .78))
    line(root, "edge-commit-output", "commit", "output", PINK, "结果回传",
         exit=(1, .7), entry=(0, .7))
    line(root, "edge-commit-buffer", "commit", "buffers", PINK, "不可变写回", dashed=True,
         points=[(1785, 1140), (1785, 897.75)], exit=(1, .273), entry=(1, .85))
    line(root, "edge-success-ready", "commit", "ready", PURPLE, "唤醒后继", dashed=True,
         exit=(.5, 0), entry=(1, .8), points=[(1640, 965), (1500, 965), (1500, 775), (1225, 775), (1225, 730)], label_pos=.4)
    line(root, "edge-result-supervisor", "result-gate", "supervisor", PURPLE, "完成 / 异常", dashed=True,
         exit=(1, .24), entry=(.5, 1), points=[(1248, 1147.8), (1375, 1147.8)])
    line(root, "edge-control-supervisor", "control-input", "supervisor", PURPLE, "取消 / 期限", dashed=True,
         entry=(1, .5), points=[(1845, 900), (1845, 1047.5)])
    line(root, "edge-supervisor-ledger", "supervisor", "ledger", PURPLE, "退出确认后释放资源", dashed=True,
         exit=(0, .5), entry=(1, .8), points=[(950, 1047.5), (950, 866)])
    line(root, "edge-supervisor-ready", "supervisor", "ready", PURPLE, "重试 / 跳过", dashed=True,
         exit=(.5, 0), entry=(1, .5), points=[(1375, 810), (1210, 810), (1210, 700)])
    line(root, "edge-supervisor-status", "supervisor", "commit", PURPLE, "失败状态", dashed=True,
         exit=(1, .8), entry=(.3, 0), points=[(1588, 1082)])
    # A self-contained legend keeps the new control/status extension distinct from
    # the original red analysis-data semantics.
    node(root, "legend", "线型图例", 30, 1360, 2200, 130, "#f5f5f5", "#b3b3b3",
         "align=left;verticalAlign=top;fontStyle=1;fontSize=17;")
    for index, (color, label, dashed) in enumerate([
        (BLUE, "原始数据", False), (BLACK, "标准数据 / 张量", False), (RED, "分析数据 / 依赖报告", False),
        (GREEN, "执行任务 / 处理单元数据", False), (PINK, "结果回传 / 快速通道", False),
        (PURPLE, "控制 / 状态 / 异常反馈", True),
    ]):
        x, y = 65 + (index % 3) * 700, 1410 + (index // 3) * 44
        a = node(root, f"legend-a-{index}", "", x, y, 1, 1, "none", "none", "opacity=0;")
        b = node(root, f"legend-b-{index}", "", x + 95, y, 1, 1, "none", "none", "opacity=0;")
        sample = line(root, f"legend-line-{index}", a.get("id"), b.get("id"), color,
                      exit=(1, .5), entry=(0, .5), dashed=dashed)
        sample.set("style", sample.get("style").replace("edgeStyle=orthogonalEdgeStyle;", "edgeStyle=none;noEdgeStyle=1;"))
        text(root, f"legend-label-{index}", label, x + 115, y - 14, 520, 28, 16)
    text(root, "detail-footer", "对应代码：ParallelLogicDeterminationAlgorithModule / plda  ·  设备内核心分配由 OS / 厂商运行时完成",
         30, 1510, 2220, 24, 14, "#666666")
    # Validate editable graph topology before replacing the user's file.
    for diagram in mxfile.findall("diagram"):
        graphroot = diagram.find("mxGraphModel/root")
        ids = [c.get("id") for c in graphroot]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate cell id")
        for cell in graphroot:
            for attr in ("parent", "source", "target"):
                if cell.get(attr) is not None and cell.get(attr) not in ids:
                    raise ValueError(f"dangling {attr}: {cell.attrib}")
    ET.indent(tree, space="  ")
    temp = FILE.with_suffix(".drawio.tmp")
    tree.write(temp, encoding="utf-8", xml_declaration=False)
    temp.replace(FILE)
    print(f"Updated {FILE}; pages={len(mxfile.findall('diagram'))}; backup={BACKUP}")


if __name__ == "__main__":
    main()
