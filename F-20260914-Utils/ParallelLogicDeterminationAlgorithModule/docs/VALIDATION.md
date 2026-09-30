# 验证记录

验证日期：2026-09-14。真实本机执行；没有用 CPU 结果冒充 GPU/NPU 成功。

后续增加了 [2048×2048 大数据 smoke](LARGE_DATA_SMOKE.md)：四条真实设备路径的矩阵乘法和加法全部通过，并分别记录当前 PLDA 完整作业与独立上下文复用实验的耗时。

## 环境

- Windows；Python 3.14.7。
- CPU：Intel Core Ultra 7 255HX，20 个逻辑处理器。
- NVIDIA GPU：GeForce RTX 5060 Laptop GPU，约 8 GB 显存；驱动 592.01，`nvidia-smi` 报告 CUDA 支持 13.1。
- Intel GPU：Intel Graphics；设备驱动 32.0.101.8724。
- NPU：Intel AI Boost，Windows PnP 状态 OK。
- 项目内 `.venv`：NumPy 2.5.3、CuPy 14.2.0、OpenVINO 2026.3.1、cuda-toolkit 13.1.2.0。具体依赖见 `requirements-hardware.lock.txt`。

只安装了项目虚拟环境里的依赖与本地包，没有更换显卡/NPU 驱动。CuPy 实际查询到的 runtime version 为 13020；组件解析可能使用主机上已有的兼容运行库，依赖锁不代表完整系统驱动/动态库镜像。

## 单元及进程集成测试

最终运行 **29 项测试，全部通过，无跳过**，用时约 5.03 秒。日志：[unit-test-results.txt](unit-test-results.txt)。

覆盖：

- RAW/WAR/WAW、未知别名、区间不相交、未知效果、屏障、依赖环；
- ILP 延迟、发射宽度、执行端口、占用周期及关键路径；
- SIMD 大循环等步长证明、跨迭代反例、未知/预算上限、浮点归约；
- 300 组固定种子的仿射循环，对照独立穷举 oracle 检查判定；
- 真实 CPU 工作进程重叠执行、前驱提交后才能启动后继；
- 槽位/内存背压、主机预算、图输入验证；
- 实际任务超时终止、作业超时、活动及排队取消；
- 内核异常、独立分支继续、后继跳过；
- 故障注入：工作进程通过 `os._exit(23)` 异常退出，回收后换执行配额重试；
- 不存在的 NPU 目标触发不可用错误并回退 CPU；不会把回退记作 NPU 执行；
- 不可变请求快照、跨提交配额、分片尾部/空向量、NumPy 与标量内核对照。

## 四条真实设备路径

执行 `python -m tests.hardware_smoke`，每条路径用**只有该设备的独立模块清单**运行，不存在自动回退到 CPU 的候选。每台设备运行 `add/multiply/scale/relu/identity/matmul/sum` 共 7 个算子，输入是 64×64 非恒定、有正负值的 float32 张量。

| 设备 | 实际后端 | 算子结果 | 测试作业耗时 | 该测试集最大绝对误差 |
| --- | --- | --- | --- | --- |
| CPU | NumPy native | 7/7 通过 | 0.630 s | 0 |
| RTX 5060 Laptop GPU | CuPy / CUDA | 7/7 通过 | 4.709 s | 0 |
| Intel Graphics | OpenVINO `GPU.0` | 7/7 通过 | 45.104 s | 0 |
| Intel AI Boost | OpenVINO `NPU` | 7/7 通过 | 38.287 s | 0 |

合计 **28 个真实设备算子检查通过**。完整设备清单、数值检查、实际执行模式、分配/提交事件见 [hardware-validation.json](hardware-validation.json)。

这里的耗时包括逐任务进程启动、首次编译/缓存与数据搬运，且没有做稳定重复测量，**不是 CPU/GPU/NPU 性能排名或加速比基准**。测试使用可精确表示的二进制分数；零误差只描述这组数据，不表示加速器对任意输入都位级等价。测试验收阈值是 `abs(error) <= 1e-4 + 1e-3 * abs(reference)`。

OpenVINO 同时枚举出了 NVIDIA 显卡的 OpenCL 入口。本模块的自动探测排除非 Intel 的 OpenVINO GPU，将 NVIDIA 交给 CUDA 后端，防止对同一块显卡重复配额。

## 单作业异构混合调度

执行 `python -m tests.heterogeneous_smoke`：同一个模块内将三个独立 scale 任务分别限制到 CPU/GPU/NPU，随后在 CPU 合并成 `9*x`。

- 作业成功，结果逐项一致；
- 三个根任务从分配到提交的时间区间重叠；
- 合并任务只在需要的前驱提交后启动；
- 完整作业约 1.39 秒，事件和输出见 [heterogeneous-validation.json](heterogeneous-validation.json)。

区间重叠证明监督器同时派发并维持多个设备任务在途，包括启动/传输/编译阶段；未使用厂商 kernel 时间线分析器，不能据此声称每个硬件内核都在同一时刻执行。

## 复现

在模块目录执行：

```powershell
# 已创建的环境
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m tests.hardware_smoke --output docs/hardware-validation.json
.\.venv\Scripts\python.exe -m tests.heterogeneous_smoke
.\.venv\Scripts\python.exe -m plda analyze examples/request.json --output examples/analysis-report.json
.\.venv\Scripts\python.exe -m plda run examples/request.json --output examples/execution-result.json

# 新机器：先确认厂商驱动兼容，再安装同一组项目依赖
.\scripts\install-hardware.ps1 -UseLock
```

硬件测试是显式选择的测试入口，不会在日常 unittest 中自动编译或占用所有加速器。无某类硬件时不能宣称该类测试通过。不同机器上的具体算子/shape 支持仍需验证。

## 尚未验证或实现的范围

没有宣称完成真实 ISA 的二进制解码/机器码生成、LLVM 自动接入、多节点通信、任意业务副作用事务、驱动级硬内存隔离、持久 worker、GPU/NPU 常驻数据缓存、通用图分区、任意循环优化以及最优调度。已完成部分及其前提详见 [DESIGN.md](DESIGN.md)。
