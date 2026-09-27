# DS 循环进度台账

<!-- 本文件由定时任务「DS 阶段循环调度」读写。状态字段请勿手工改动； -->
<!-- 如需干预，请同时修改「人工备注」一节，便于下次检查留痕。 -->

## 循环参数

- check_interval_minutes: 30
- stage_total: 18
- stage_sequence: P00 → P01 → P02 → P03 → P04 → P05 → P06 → P07 → P08 → P09 → P10 → P11 → P12 → P13 → P14 → P15 → P16 → P17

### 调度器配置

平台不支持 `MINUTELY` 频率，`BYMINUTE` 亦被忽略，单条规则的最小粒度是 1 小时。因此 30 分钟节奏由**两个互为错峰的每小时任务**实现：

| 任务 ID | 名称 | 触发分钟 | validFrom |
|---|---|---|---|
| `354b1fc5-eec1-446c-a412-768a3a6dbffa` | DS 阶段循环调度（每30分钟检查） | 每小时 :58 | — |
| `79cba281-33da-47cf-b6a4-5e2bbf9d7f0f` | DS 阶段循环调度（每30分钟检查·错峰） | 每小时 :28 | 2026-09-22T17:28:00 |

两任务共用本台账，依靠 `last_check` 的 10 分钟重叠判定去重，避免重复推进阶段。
**维护提示**：修改或停用任一任务时，必须同步处理另一个，否则节奏会退化为 60 分钟。

> ⚠️ **权威状态源已变更**：自脚本 `scripts/ds_stage_loop.py` 接管后，
> 真实状态以 `.workbuddy/ds-loop/state.json` 为准。下表仅为快照，不再自动更新。
> 脚本产物：`current-prompt.txt`（当前阶段工单）、`last-report.md`（本轮报告）、
> `loop-log.jsonl`（日志）、`worker.lock`（工作租约）。

## 运行状态（快照 · 以 state.json 为准）

- current_stage: P17
- stage_status: done
- last_check: 2026-09-27T19:58+08:00
- last_stage_update: 2026-09-27T19:58+08:00
- consecutive_no_change: 0
- blocked_reason: —

> 快照更新于 2026-09-27：P00–P17 全部通过。门禁结果：1588 项测试、2 项既有 M01 失败（放行）、新增失败 0 项；
> 交付物齐全、阶段测试已补充、冻结目录未改动、无新增 skip/xfail。终态幂等短路生效（复跑 0.14s 秒退）。

## 两段式调度（自 2026-09-22 起）

两个定时任务已由「只检测」升级为「检测 + 实现」两段式：

1. **检测与推进**：运行 `ds_stage_loop.py`；`blocked`/`overlap`/`all_done` 停止，`updated`/`nochange` 进入第 2 段。
2. **实现当前阶段**：运行 `ds_stage_loop.py --work-order`；输出 `[WORK-ORDER]` 即按工单实现，
   `[LEASE-DENIED]` 表示另一实例正在实现，本实例停止。

互斥靠 `worker.lock` 工作租约：同一阶段同时只允许一个实例实现，推进时自动释放，超时（55 分钟）自动失效。

`stage_status` 取值：`in_progress`（等待增量落地）｜`done`（增量已验收）｜`blocked`（回归或越界，需人工介入）

## 基线

- unittest_result: 未运行
- demo_result: 未运行
- baseline_recorded_at: —

## 交付物指纹（当前阶段）

<!-- 由检查任务对本阶段「预期输出」声明的文件计算 sha256 前 12 位；文件不存在记为 MISSING。 -->
<!-- 指纹与台账不一致 → 判定为发生了一次版本更新，进入验收。 -->

| 阶段 | 文件 | sha256(12) | 记录时间 |
|---|---|---|---|
| P00 | INTERFACES.md | MISSING | 2026-09-22T16:51+08:00 |

## 阶段完成记录

| 阶段 | 阶段名 | 状态 | 完成时间 | 交接块摘要 |
|---|---|---|---|---|
| P00 | 基线锚定与接口冻结 | in_progress | — | — |
| P01 | 受限表达式语法与规范化 | pending | — | — |
| P02 | 候选变量枚举与定义域追踪 | pending | — | — |
| P03 | 表示构造管线与去重剪枝 | pending | — | — |
| P04 | 关系与方程搜索 | pending | — | — |
| P05 | 结构模式搜索 | pending | — | — |
| P06 | 稳定性与重采样评估 | pending | — | — |
| P07 | 假说对象与不可覆盖版本 | pending | — | — |
| P08 | 模式→假说与竞争解释 | pending | — | — |
| P09 | 多维评估指标 G/S/N/C | pending | — | — |
| P10 | Pareto 筛选与候选池管理 | pending | — | — |
| P11 | 冻结计划生成与确证绑定 | pending | — | — |
| P12 | 检验执行与跨轮误差预算 | pending | — | — |
| P13 | 结果三分类、记录与发布 | pending | — | — |
| P14 | 主动取证与反例搜索 | pending | — | — |
| P15 | 知识归档与假说更新 | pending | — | — |
| P16 | 端到端主循环集成 | pending | — | — |
| P17 | 自主取数执行器 | pending | — | — |

## 调度输出存档

<!-- 每次判定为「已更新」时，本轮生成的下一轮上下文包与提示词摘要追加到此处。 -->

（尚无记录）

## 人工备注

（无）
