# PLDA 接口 v1

## Python 模块边界

| 方法 | 输入 | 输出/行为 |
| --- | --- | --- |
| `PLDAModule(devices=None, config=None)` | 可选设备清单与资源策略 | 默认探测实际设备；建立有界作业队列 |
| `capabilities()` | 无 | 版本、分析类型、设备、探测诊断、隔离设备 |
| `analyze(program, machine=None)` | `Program`，可选 `MachineModel` | `Plan`：TLP 图、ILP 发射计划、SIMD 判断、buffer 规格、优先级 |
| `submit(program)` | 完整、合法的任务图 | `JobHandle`，异步执行 |
| `run(program)` | 同上 | 等待并返回 `JobResult` |
| `close(cancel=False)` | 是否取消未完成作业 | 停止接收并等待清理 |
| `handle.status()` | 无 | 作业状态、各任务状态/尝试次数、最近 20 条事件 |
| `handle.cancel()` | 无 | 申请取消；已终态时返回 False |
| `handle.result(timeout=None)` | 最长等待时间 | 完整结果；等待超时不改变作业执行 |

`analyze_ilp(instructions, machine)`、`analyze_simd(loop, max_access_bytes)`、`analyze_tlp(operations, costs)` 可单独使用，不需要启动模块或安装硬件软件。`analyze_tlp` 的操作提供 `id/accesses/depends_on/effects_complete/barrier` 属性，使用 `Instruction` 即可表达与张量内核无关的任务契约。

## 请求信封

```json
{
  "version": 1,
  "source_kind": "standard",
  "inputs": {
    "x": {"shape": [3], "dtype": "float32", "values": [1, 2, 3]}
  },
  "tasks": [
    {
      "id": "out",
      "op": "scale",
      "inputs": ["x"],
      "params": {"factor": 2},
      "numerical_mode": "relaxed",
      "devices": ["CPU", "GPU", "NPU"],
      "timeout_s": 30,
      "max_retries": 1
    }
  ],
  "outputs": ["out"],
  "instructions": [],
  "loops": []
}
```

只接受版本 1；未知顶层字段拒绝。`source_kind` 为 `raw/standard/analysis`，是输入来源标签，不改变正确性规则。`inputs` 的值可直接为数字数组，标准化成一维 float64；明确的张量对象可指定 shape/dtype。`outputs` 不可为空；允许零任务直接返回已存在输入。

`Task` 必填 `id/op/inputs`，其余字段：

| 字段 | 默认 | 含义 |
| --- | --- | --- |
| `params` | `{}` | 算子参数，严格检查字段 |
| `depends_on` | `[]` | 额外完成先后约束 |
| `accesses` | `[]` | 额外资源读写声明；张量访问会自动加入 |
| `effects_complete` | `true` | 已完整声明效果；false 时按输入顺序串行约束 |
| `barrier` | `false` | 不允许跨越的控制/异常/外部顺序屏障 |
| `devices` | CPU/GPU/NPU | 允许设备种类，回退不得越过此许可 |
| `numerical_mode` | `strict` | `strict` 标量 CPU 参考；`relaxed` 后端浮点语义 |
| `estimated_ms` | `{"CPU":1}` | 可配置计算成本；缺失设备成本沿用 CPU，不猜测倍率 |
| `timeout_s` | `30` | 一次尝试的墙钟期限 |
| `max_retries` | `1` | 最多额外尝试次数；仅限其他仍可用设备 |
| `workspace_bytes` | `0` | 调用者预留的额外 workspace |

`scale` 必须提供 `factor`；`slice` 必须提供 `start/stop`；其他当前算子不接受参数。修改 `.params` 字典不会影响已提交作业：submit 会快照请求。

## 指令及循环 IR

资源访问：`{"resource":"allocation:x","mode":"read","start":0,"end":16}`。省略 start/end 表示整个分配；resource=null 表示未知别名。寄存器及 flags 用规范名字作为资源，例如 `register:rax`、`flags:eflags`；部分寄存器重叠要映射到相同分配的区间。

```python
from plda import Access, Instruction, MachineModel, analyze_ilp

instructions = (
    Instruction("load", (Access("mem:x", "read"), Access("reg:r1", "write")),
                latency=4, unit="load"),
    Instruction("independent", (Access("reg:r2", "write"),), latency=2),
    Instruction("use", (Access("reg:r1", "read"),)),
)
report = analyze_ilp(instructions, MachineModel(issue_width=2, units={"alu": 2, "load": 1}))
```

`Instruction` 还支持 `depends_on/occupancy/effects_complete/barrier`。任何未解析的指令效果必须标未知，不能用空访问列表替代未知效果。

```python
from plda import AffineAccess, Loop, analyze_simd

loop = Loop("scale", 100003, (
    AffineAccess("allocation:input", "read", stride=4, offset=0, width=4),
    AffineAccess("allocation:output", "write", stride=4, offset=0, width=4),
), vector_width=8)
print(analyze_simd(loop))
```

循环索引从 0 开始。字节地址范围由 `stride*i+offset` 和 width 定义。JSON 中以对应字段构造 `instructions` 和 `loops`，未知字段会拒绝；循环还支持 `control/target_supports_masks/reduction/dtype/allow_reassociation`。详见 model.py 的默认值和 DESIGN.md 的证明前提。

## 返回和事件

作业状态：`QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED/TIMED_OUT`。任务状态：`PENDING/RUNNING/SUCCEEDED/FAILED/SKIPPED/CANCELLED`。

`JobResult.to_dict()` 包括：

- `job_id/state/elapsed_s`；
- `outputs`：仅已提交的请求输出，张量以 shape/dtype/values 表达；失败作业可能有成功独立分支的输出；
- `task_states/attempts/errors`；
- `events`：`JOB_STARTED/RESERVED/RELEASE/COMMITTED/ATTEMPT_FAILED/RETRY_PENDING/DEVICE_QUARANTINED/PLACEMENT_FAILED/SKIPPED/JOB_FINISHED` 等。

事件时间为作业激活后的单调时钟秒数。`RESERVED` 包含设备 ID、占用字节、使用槽位、尝试编号、预测成本。`COMMITTED` 包含实际执行后端、数据字节数与 worker 耗时。事件是诊断数据，不是对外分布式事务日志。

错误码包括 `NO_ELIGIBLE_DEVICE/HOST_BUDGET_EXCEEDED/BACKEND_UNAVAILABLE/OUT_OF_MEMORY/WORKER_CRASH/TASK_TIMEOUT/KERNEL_ERROR/INVALID_EXECUTION/INVALID_RESULT/DEPENDENCY_FAILED/SUPERVISOR_ERROR`。作业超时以 `TIMED_OUT` 状态和对应事件表达。

CLI：`analyze/run REQUEST --output FILE` 写 JSON；`--cpu-only` 显式只使用 CPU；成功退出码 0，输入或执行失败退出码 2。设备信息通过 `python -m plda devices` 查询。
