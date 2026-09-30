# 验证记录

## 2026-09-09：endpoint 与 Key 切换

环境：Windows，Python 3.14.7。当前目录没有 Git 元数据，本次依据现有文件检查和实施变更，无法还原此前的 Git 差异。

- 修改前基线：76 项测试，75 项通过，1 项符号链接测试因 Windows 权限跳过。
- 修改后 `python -m unittest discover -s tests`：100 项测试，99 项通过，同一项符号链接测试跳过。
- 新增覆盖两种 provider 的 endpoint/Key 配对、命令行覆盖 JSON、指定 Key 环境变量缺失时禁止回退、默认 Codex 登录兼容、子工作目录连接继承与预算共享、密钥不进入 Codex 参数和运行记录，以及响应错误和请求 ID 脱敏。
- 内部运行配置文件（包括文件名含方括号的配置）通过真实快照测试验证被排除；外部同名配置不会误排除项目文件。
- `python -m renew demo --output <新的临时目录>`：完整离线流程通过，两个需求均通过测试与独立模拟审查。
- `python -m renew run --help` 和 `python -m compileall -q renew tests` 通过。
- 两个 JSON 配置通过加载校验；逐字段比较确认既有 provider、模型、预算及 base_url 均保留，只新增 api_key_env 和 api_key。

连接测试使用合成 Key 和模拟 HTTP/Codex 进程；未使用真实 Key 请求外部服务。本次验证证明连接选择、传递及流程回归通过，不代表任意服务的网络、账户或模型可用性已验证。

## 2026-09-08：历史验证

验证日期：2026-09-08。环境：Windows，Python 3.14.7。

- `python -m unittest discover -s tests -v`：共 66 项，65 项通过；1 项符号链接测试因 Windows 不允许创建符号链接而跳过。
- 离线端到端示例：模拟分析、规划、开发及审查角色，真实复制代码、完成两项需求并运行测试；示例最终 6 项测试通过，原始示例项目不变。
- 集成补丁已由端到端测试执行 `git apply --check` 验证可应用，包含 Windows 换行处理。
- 已覆盖代码引用造假、依赖环、越界写入、缺失验收条件、前置成果保留、API 失败记录、预算、工具调用协议及测试副作用隔离。
- 已检查命令行帮助与 Python 编译。

历史验证中的直接 Responses API 调用未连通。当前实现已改用标准 Codex CLI，仍需通过一次真实只读调用验证当前账户、模型和网络配置。离线 provider 的固定输出不能证明真实模型效果。

确认 `codex login status` 成功后，可先执行：

```powershell
python -m renew run examples/tiny_project --analyze-only
```

再按 README 配置目标项目的测试命令，运行真实开发与验收流程。
