# 系统模块与 PLDA 架构图

项目根目录的 `TheStructure.drawio` 是原生可编辑源文件，现有 **5 页**。

模块定义遵循用户指定的绘图逻辑：**除背景板外，不被其他区域包含的最大区域**。因此一级模块只有 **DEVICE_1、通信基础设施、数据处理模块**。PLDA、负载均衡、路由、标准化转换、CORE 等均为这些模块包含的子区域。

| 页码 | 页面 | 内容 |
|---|---|---|
| 1 | 系统总览 | 同页展示三个模块及原有内部子区域、跨模块数据线；可跳转到三个独立页 |
| 2 | PLDA 架构与数据流 | 保留原有详图，内容逐字节不变 |
| 3 | DEVICE_1 | CORE_1、CORE_2、CORE_3、CORE_X 与来自通信基础设施的任务分发接口 |
| 4 | 通信基础设施 | 负载均衡 / PLDA、标准化转换、路由和结果回传；两侧标出另两个模块的外部接口 |
| 5 | 数据处理模块 | 原始数据、标准数据、标准分析数据、快速数据通道、统计及分析模型与通信接口 |

一级模块的独立页由原总图包含的区域提取，保留全部原有子节点及内部连线；跨模块连线仍沿用原端点含义、方向和颜色，只将另一个模块中的端点表示为可跳转的外部接口框。外部接口说明框不视为本页新增模块。通信基础设施页和总览中的 PLDA 节点可跳转到保留的第 2 页。

蓝色表示原始数据，黑色表示标准数据，红色表示分析数据，绿色表示执行任务 / 设备数据，洋红色表示结果 / 快速通道。第二页原有的紫色控制、状态与异常反馈线也保持不变。

文件与验证记录：

- `system-overview.png`：第 1 页总览预览。
- `plda-architecture.png`：原 PLDA 详图预览。
- `system-device.png`、`system-communication.png`、`system-data-processing.png`：三个独立页预览。
- `module-pages.json`：三模块定义、成员节点 ID、内部和跨模块连线 ID、分页和保留校验。
- `module-pages-validation.json`：最终验证结果。
- `TheStructure.before-system-module-pages-20260915.drawio`：本次纠正模块定义前的完整文件备份。
- `TheStructure.before-module-pages-20260915.drawio`：此前只有总览和 PLDA 详图时的两页备份。
- `TheStructure.before-plda-20260915.drawio`：最初的总图备份。
- `archive-plda-functional-pages/`：上一次误按 18 个 PLDA 内部职责拆分的预览和脚本存档；这些分页已从当前源文件移除。

`scripts/draw_system_module_pages.py` 从本次纠正前备份的第一页重建总览和三个模块分页，并保留当前文件中 `plda-architecture-dataflow-v1` 页的完整内容，将它放在第二页。脚本检查模块归属、原总览 ID 保留、连线端点有效和页面跳转有效。旧入口 `scripts/draw_plda_module_pages.py` 已转接到此脚本，避免再次生成错误层级的分页。

本次只调整图形组织与分页，不更改 PLDA 运行代码。所有预览均通过本机 draw.io 导出器从 XML 源文件渲染。
