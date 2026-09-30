# PLDA 并行逻辑判断与异构调度模块

本模块对应项目 `TheStructure.drawio` 的 **PLDA**（节点 `8rw-NWJHrkYK3E8QMIOX-83`）。提供可运行的 Python 包、版本化 JSON 接口、ILP/TLP/SIMD 分析器，以及 CPU / NVIDIA GPU / Intel GPU / Intel NPU 调度后端。

这是有明确正确性边界的第一版实现。对完整 IR 给出依赖证明、保守顺序或未知结论；不会声称能自动理解任意源程序。GPU/NPU 使用实际厂商运行时，设备不可用时返回原因或按任务许可回退。

## 使用

在本文件所在目录执行。Python 3.11+，CPU 标量路径只依赖标准库。项目内 `.venv` 用于本机加速器测试，运行它可使用已安装的加速器依赖。

```powershell
# 标准库 CPU 路径
python -m plda analyze examples/request.json --cpu-only
python -m plda run examples/request.json --cpu-only
python -m unittest discover -s tests -v

# 已安装依赖的项目环境：探测与实际调度
.\.venv\Scripts\python.exe -m plda devices
.\.venv\Scripts\python.exe -m plda run examples/request.json
.\.venv\Scripts\python.exe -m examples.demo
.\.venv\Scripts\python.exe -m tests.hardware_smoke
.\.venv\Scripts\python.exe -m tests.heterogeneous_smoke
.\.venv\Scripts\python.exe -m tests.large_data_smoke --size 2048 --repeats 7
```

作为独立包接入其他项目：`python -m pip install -e .`。命令行安装入口为 `plda`；无需安装也可以从当前目录使用 `python -m plda`。

```python
from plda import PLDAModule, load_program

def main():
    request = load_program("examples/request.json")
    with PLDAModule() as module:
        plan = module.analyze(request)
        print(plan.graph.levels)       # 可同时就绪的任务层
        print(plan.ilp["schedule"])   # 指令发射周期与执行单元
        print(plan.simd)               # safe / unsafe / unknown，含依据
        handle = module.submit(request)
        print(handle.status())
        result = handle.result(timeout=60)
        print(result.state, result.outputs["result"].values())
        # handle.cancel() 申请取消；result(timeout=...) 仅限制等待时间。

if __name__ == "__main__":
    main()
```

Windows 使用 `spawn` 启动工作进程，调用执行 API 的入口必须有上述 `__main__` 保护。在交互笔记本中建议通过 CLI 运行脚本。

## 已实现的能力

| 层次 | 输入 | 分析/执行结果 |
| --- | --- | --- |
| 指令级 ILP | 指令的寄存器/标志位/内存访问、显式依赖、屏障、延迟、执行单元 | RAW/WAR/WAW 图；考虑发射宽度、单元数、占用周期的列表调度；周期/IPC 估计 |
| 线程级 TLP | 操作访问契约、控制依赖；或可执行张量 DAG | 依赖图、拓扑层、Work/Span、就绪任务；运行时以独立进程实现 CPU 并行，绕开 Python GIL |
| SIMD | 有限单循环、仿射字节地址、控制/归约契约、向量宽度 | 等步长代数证明或有预算的精确访问检查；冲突反例；向量轮数与标量尾部 |
| 数据分配 | 不可变张量、形状、dtype、内存预算 | 主机到设备暂存、设备到主机返回、成功后提交；逐元素向量分片与有序拼接 |
| 资源调度 | 设备槽位、内存配额、延迟/搬运成本、任务设备许可 | 关键路径优先；估计完成成本选设备；资源不足等待；永久不匹配失败 |
| 异常 | 启动失败、OOM、内核错误、超时、崩溃、取消 | 有限重试/换设备、受影响后继跳过、独立分支继续、未提交结果丢弃、异常设备隔离 |

**ILP 和 SIMD 报告是分析与计划，不是机器码生成器。** 本模块不直接操纵 CPU 发射端口，不把 Python 循环称为 SIMD。`strict` 使用标量参考语义；`relaxed` CPU 使用 NumPy 原生运算（若可用），实际 SIMD 指令选择由 NumPy/编译器完成；CUDA GPU 是设备内核执行，不能把 GPU 的 SIMT 与 CPU SIMD 混为一谈。

## 执行内核与数据契约

- 支持 `identity`、`add`、`multiply`、`scale`、`relu`、二维 `matmul`、全量 `sum`、一维 `slice`、一维 `concat`。
- 张量是小端连续 `float32` / `float64` 不可变缓冲区。形状必须精确匹配，不隐式广播或转换 dtype。
- 每个任务 ID 同时是唯一输出 ID。输入必须先定义，任务输出只写一次。
- `strict`：Python binary64 标量运算，顺序左折叠求和，最后转换输出 dtype；不是对任意 C/硬件浮点语义的承诺。浮点溢出等错误会结构化报告。
- `relaxed`：调用者接受后端的浮点舍入、归约顺序、融合/内部精度和特殊值处理差异。GPU/NPU 需要显式选择此模式；没有全局误差上界承诺。
- OpenVINO 适配器支持非空 float32 的 `identity/add/multiply/scale/relu/matmul/sum`。实际 shape/算子能否在设备上编译，还由运行时验证。
- 原始输入只接受 JSON 数值数组的明确标准化。不推断图片、任意文件或业务逻辑的语义。

`analyze_tlp()` 可以分析自定义操作契约；执行器只接受上述已审阅的纯内核。外部 IO、退款、发送消息等不可逆副作用不进入自动重试路径。

## 资源配置与取消

```python
from plda import Device, PLDAModule, RuntimeConfig

devices = [
    Device("cpu:0", "CPU", "cpu", "host", slots=4, memory_bytes=1024**3),
    Device("cuda:0", "GPU", "cupy", "0", slots=1, memory_bytes=2 * 1024**3),
    Device("npu:0", "NPU", "openvino", "NPU", slots=1, memory_bytes=512 * 1024**2),
]
config = RuntimeConfig(max_jobs=16, max_tasks=1024,
                       host_buffer_bytes=1024**3, job_timeout_s=300)
# 只配置实际存在的设备。通常直接 PLDAModule() 自动探测即可。
```

模块内部是有界 FIFO 作业队列，每次激活一个作业，其就绪任务并行运行，因此多个提交不会重复使用同一配额。不同模块实例需要上层分配互不重叠的配额。`max_jobs` 包含排队及运行中的作业，队满时同步拒绝，由上游施加背压。

内存预算是**逻辑张量及显式 workspace 的准入预算**，不是进程 RSS/驱动显存的硬限制；Python 对象、进程启动、厂商编译缓存等还有开销。实际分配失败有独立 OOM 路径。每个尝试启动一个进程，适合验证及较粗任务；小任务应先合并或调整分片大小，不能据此承诺加速。

`task.timeout_s` 包含进程启动和结果准备；作业期限从激活时开始，不含排队等待。取消会终止活动工作进程并确认退出，再释放配额；加速器超时/崩溃后本模块实例隔离该设备。排队作业的取消在轮到它时兑现；关闭默认等待，`close(cancel=True)` 取消所有未完成作业。

## 文件

- [设计与正确性说明](docs/DESIGN.md)
- [JSON/API 接口说明](docs/INTERFACE.md)
- [本机验证记录](docs/VALIDATION.md)
- [2048×2048 大数据 smoke](docs/LARGE_DATA_SMOKE.md)
- [示例请求](examples/request.json)、[调用与分片示例](examples/demo.py)
- `plda/model.py`：操作、指令、循环、张量、设备 IR
- `plda/analysis.py`：ILP/TLP/SIMD 分析
- `plda/planner.py`：数据流验证、能力匹配、优先级/成本
- `plda/runtime.py`：可调度模块、资源账本、生命周期
- `plda/backends.py`：真实硬件适配与隔离工作进程
- `tests/`：确定性单元测试、CPU 进程集成测试

项目根目录 `TheStructure.drawio` 已包含 PLDA 子模块概览及第 2 页“PLDA 架构与数据流”，沿用总图颜色并补充数据回传和控制反馈。JSON 边界及 Python API 是 PLDA 的接入契约；图中上游路由/标准化系统并未因此实现。

本机已完成 29 项自动测试、CPU/RTX GPU/Intel GPU/NPU 四条路径共 28 个算子检查，以及 CPU/GPU/NPU 混合调度测试。安装依赖、硬件型号、验证边界和原始报告见验证记录。
