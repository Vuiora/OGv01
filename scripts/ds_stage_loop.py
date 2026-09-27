#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""SDL 阶段循环调度器（DS Stage Loop）。

职责（严格限定，不代替人做实现）：
  1. 探测上一阶段是否完成一次版本更新（交付物指纹比对，非修改时间）；
  2. 更新后执行验收门禁（测试全绿 / 交付物齐全 / sdl_m01 未被改动 / 无新增 skip-xfail）；
  3. 通过则推进阶段并组装下一轮提示词，写入 .workbuddy/ds-loop/next-prompt.txt；
  4. 在 --dispatch 且配置允许时，以 headless 方式调用 WorkBuddy CLI 发起下一轮。

设计约束：
  - 只写 .workbuddy/ds-loop/ 下的文件；绝不修改源码或 sdl_m01/；
  - 不读取、不打印任何令牌、证据库或受限质量报告；
  - 门禁不通过就停在原地，绝不跳过失败测试。

依赖：Python 3.11+ 标准库。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8))
# 本文件位于 <repo>/scripts/ds_stage_loop.py，故根目录为上两级。
ROOT = Path(__file__).resolve().parent.parent
LOOP_DIR = ROOT / ".workbuddy" / "ds-loop"
STATE_PATH = LOOP_DIR / "state.json"
STAGES_PATH = LOOP_DIR / "stages.json"
NEXT_PROMPT_PATH = LOOP_DIR / "next-prompt.txt"
# 当前阶段的工单：每次运行都重写，供执行方（定时任务的 agent）直接照做。
# 与 next-prompt.txt 的区别：后者只在「推进」瞬间写入，描述的是下一阶段；
# 而本文件在任何判定下都反映 state 里的当前阶段，是执行方的唯一权威工单。
CURRENT_PROMPT_PATH = LOOP_DIR / "current-prompt.txt"
# 工作租约：两个错峰定时任务可能同时想「实现当前增量」，必须互斥。
# 抢到租约的实例才允许动交付物；没抢到的只做检测与上报。
WORKER_LOCK = LOOP_DIR / "worker.lock"
REPORT_PATH = LOOP_DIR / "last-report.md"
LOG_PATH = LOOP_DIR / "loop-log.jsonl"
DISPATCH_LOG = LOOP_DIR / "dispatch"

VERDICT_UPDATED = "updated"
VERDICT_NOCHANGE = "nochange"
VERDICT_BLOCKED = "blocked"
VERDICT_OVERLAP = "overlap"
VERDICT_DONE = "all_done"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def load_json(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"[ERR] 缺少配置文件：{path}")
    # 用 utf-8-sig 读取：Windows 下编辑器/PowerShell 常写入带 BOM 的 UTF-8，
    # 直接用 utf-8 解析会在首个字符处抛 JSONDecodeError。
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"[ERR] 配置文件不是合法 JSON：{path}\n      {exc}"
        )


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def append_log(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def validate_config(stages_cfg: dict) -> list[str]:
    """配置一致性校验。返回警示信息列表（不阻断执行）。

    最重要的一条：阶段交付物若位于冻结目录内部，创建交付物本身就会被判为越界，
    形成永远无法通过的门禁。这是常见配置陷阱，必须提前提示。
    """
    warns: list[str] = []
    frozen = [str(p).replace("\\", "/").strip("/") for p in stages_cfg.get("frozen_paths", [])]
    for st in stages_cfg.get("stages", []):
        for out in st.get("outputs", []):
            o = str(out).replace("\\", "/").strip("/")
            for fr in frozen:
                if o == fr or o.startswith(fr + "/"):
                    warns.append(
                        f"阶段 {st['id']} 的交付物 {out} 位于冻结目录 {fr} 内，"
                        f"创建它会被判为越界、门禁永不通过。请调整 outputs 或 frozen_paths。"
                    )
    ids = [st.get("id") for st in stages_cfg.get("stages", [])]
    if len(ids) != len(set(ids)):
        warns.append("阶段编号存在重复，state 推进可能出错。")
    for st in stages_cfg.get("stages", []):
        for d in st.get("deps", []):
            if d not in ids:
                warns.append(f"阶段 {st['id']} 依赖了不存在的阶段 {d}。")
        if not st.get("outputs"):
            warns.append(f"阶段 {st['id']} 未声明 outputs，将无法检测版本更新。")
    return warns


def fingerprint(path: Path) -> str:
    """内容指纹，取 sha256 前 12 位；文件不存在返回 MISSING。"""
    if not path.is_file():
        return "MISSING"
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()[:12]


def snapshot_outputs(stage: dict) -> dict:
    return {rel: fingerprint(ROOT / rel) for rel in stage.get("outputs", [])}


def snapshot_tests(stage: dict) -> dict:
    """记录本阶段测试交付物的基线指纹，供门禁判断「是否真的补充了测试」。"""
    return {rel: fingerprint(ROOT / rel) for rel in stage.get("tests", [])}


def file_mtime(path: Path) -> float:
    return path.stat().st_mtime if path.is_file() else 0.0


def detect_update(stage: dict, state: dict) -> tuple[bool, list[str]]:
    """判定是否发生一次版本更新。返回 (是否更新, 依据说明列表)。"""
    recorded = state.get("output_fingerprints", {})
    reasons: list[str] = []
    updated = False
    for rel in stage.get("outputs", []):
        cur = fingerprint(ROOT / rel)
        old = recorded.get(rel)
        if old is None:
            reasons.append(f"{rel}: 无基线记录，当前 {cur}")
            if cur != "MISSING":
                updated = True
        elif cur != old:
            reasons.append(f"{rel}: {old} → {cur}")
            updated = True
        else:
            reasons.append(f"{rel}: {cur}（未变）")
    return updated, reasons


def init_state(stage: dict) -> dict:
    """构造初始状态。指纹在建立基线时即固定，故已存在的交付物不会被误判为更新。"""
    return {
        "current_stage": stage["id"],
        "stage_status": "in_progress",
        "last_check": now_iso(),
        "last_stage_update": None,
        "no_change_count": 0,
        "blocked_reason": None,
        "baseline_started_at": now_iso(),
        "baseline_epoch": datetime.now(CST).timestamp(),
        "output_fingerprints": snapshot_outputs(stage),
        "test_fingerprints": snapshot_tests(stage),
        "outputs_snapshot_epoch": {o: file_mtime(ROOT / o) for o in stage.get("outputs", [])},
        "history": [],
        "baseline": {"unittest": "未运行", "demo": "未运行"},
        "prev_handoff": None,
    }


def extract_failures(text: str) -> set[str]:
    """从 unittest 输出中提取失败/错误的测试名（不含类路径，便于稳定比对）。"""
    names: set[str] = set()
    for line in text.splitlines():
        m = re.match(r"^(?:FAIL|ERROR):\s+(\S+)", line.strip())
        if m:
            names.add(m.group(1))
    return names


def run_unittest(stages_cfg: dict) -> tuple[bool, str, set[str]]:
    """运行测试。返回 (是否通过, 摘要, 失败测试名集合)。

    known_failures 中的测试允许失败（既有缺陷）；只有出现集合外的新失败才判不通过。
    """
    cmd = stages_cfg.get("gate", {}).get("unittest", ["python", "-m", "unittest"])
    known = set(stages_cfg.get("gate", {}).get("known_failures", []))
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=900
        )
    except FileNotFoundError as exc:
        return False, f"无法执行测试命令（{exc}）。请确认 python 在 PATH 中。", set()
    except subprocess.TimeoutExpired:
        return False, "测试命令超时（900 秒）。", set()

    out = (proc.stdout or "") + (proc.stderr or "")
    failures = extract_failures(out)
    new_failures = failures - known

    summary_lines: list[str] = []
    for line in out.strip().splitlines():
        if line.startswith(("Ran ", "OK", "FAILED")):
            summary_lines.append(line.strip())
    summary = "；".join(summary_lines[-3:]) or "(无摘要)"
    if known:
        summary += f"｜已知失败 {len(failures & known)} 项（放行），新增失败 {len(new_failures)} 项"
    if new_failures:
        summary += "｜新增：" + ", ".join(sorted(new_failures))

    ok = proc.returncode == 0 or not new_failures
    return ok, summary, new_failures


# 冻结目录检查需忽略的非源码产物：这些由解释器/工具自动生成或缓存，
# 其 mtime 会随「跑一次测试」而刷新，与「改动源码」无关。若不排除，
# 只要门禁自己跑过测试就会把 __pycache__ 里的 .pyc 误判为越界改动，
# 造成门禁自身触发假阳性（与之前两起「匹配过宽」缺陷同一类根因）。
_FROZEN_IGNORE_DIR_PARTS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache"})
_FROZEN_IGNORE_SUFFIXES = (".pyc", ".pyo", ".pyd")


def _is_frozen_artifact(path: Path) -> bool:
    """判断路径是否属于「非源码产物」，应从冻结目录越界检查中剔除。"""
    if any(part in _FROZEN_IGNORE_DIR_PARTS for part in path.parts):
        return True
    if path.suffix.lower() in _FROZEN_IGNORE_SUFFIXES:
        return True
    return False


def check_frozen(frozen_paths: list[str], since_epoch: float) -> tuple[bool, str]:
    """冻结目录越界检查：任何「源码文件」mtime 晚于本轮基线起点即判越界。

    只关心源码完整性，故忽略 __pycache__ 等自动生成产物（见 _is_frozen_artifact）。
    """
    if since_epoch <= 0:
        return True, "无基线时刻，跳过越界检查"
    hits: list[str] = []
    for rel in frozen_paths:
        base = ROOT / rel
        if not base.exists():
            continue
        for p in base.rglob("*"):
            if _is_frozen_artifact(p):
                continue
            if p.is_file() and p.stat().st_mtime > since_epoch:
                hits.append(str(p.relative_to(ROOT)))
    if hits:
        return False, "冻结目录被改动：" + ", ".join(sorted(hits))
    return True, "冻结目录未发现本轮改动"


def check_stage_tests(stages_cfg: dict, stage: dict, state: dict) -> tuple[bool, str]:
    """校验本阶段的测试交付物确实被补充。

    背景：只声明 outputs 时，门禁无法验证工单中「必须补充单元测试」这一要求，
    曾导致某阶段在其测试文件尚不存在的情况下被放行（当时全量测试数未变即是证据）。
    故此处要求：阶段声明的 tests 文件存在，且指纹相对基线发生了变化。
    """
    if not stages_cfg.get("gate", {}).get("require_stage_tests", False):
        return True, "未启用阶段测试校验"
    declared = stage.get("tests", [])
    if not declared:
        return True, "本阶段未声明测试交付物"

    absent = [t for t in declared if not (ROOT / t).is_file()]
    if absent:
        return False, "缺少测试交付物：" + ", ".join(absent)

    # 文件都已存在，再比对指纹：只有「相对基线发生过变化」才算真的补充了测试。
    # 基线为 MISSING 时天然表示「本轮新增」，直接视为已补充，无需比对。
    baseline = state.get("test_fingerprints", {})
    unchanged = []
    for t in declared:
        old = baseline.get(t)
        if old in (None, "MISSING"):
            continue
        if fingerprint(ROOT / t) == old:
            unchanged.append(t)
    if unchanged:
        return False, "测试交付物相对基线未变化（疑似未补充新测试）：" + ", ".join(unchanged)
    return True, "测试交付物齐全且已更新"


def _skip_xfail_markers(tree: "ast.Module") -> list[str]:
    """在 AST 中找出真正的 skip / xfail **用法**，返回带行号的描述。

    为什么用 AST 而非源码文本匹配：测试代码里出现 skip / xfail 这两个词
    未必是「跳过测试」——例如一条**守护边界**的断言
    （``assertNotIn(node.attr, ("skip", "xfail"))``）本身就在检查别人
    有没有跳过测试，其字面量纯属被测数据的名称。文本匹配会把这类断言
    误判为违规。因此这里只认真正的调用与装饰器：

    - 装饰器：``@unittest.skip``、``@unittest.skipIf``、``@pytest.mark.xfail`` 等；
    - 调用：``self.skipTest(...)``、``pytest.skip(...)``、``pytest.xfail(...)``。

    字符串常量、元组元素、属性名比较中的 ``"skip"`` / ``"xfail"`` 一律不算。
    """
    markers = {"skip", "skipIf", "skipUnless", "xfail", "skipTest"}
    hits: list[str] = []

    def _tail(node: "ast.AST") -> str:
        """取属性访问链的末段名；``pytest.mark.xfail`` → ``xfail``。"""
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Call):
            return _tail(node.func)
        return ""

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for deco in node.decorator_list:
                name = _tail(deco)
                if name in markers:
                    hits.append(f"{node.name}:@{name}")
        if isinstance(node, ast.Call):
            name = _tail(node.func)
            if name in markers:
                hits.append(f"{getattr(node, 'lineno', '?')}:{name}()")
    return hits


def check_forbidden_patterns(stages_cfg: dict) -> tuple[bool, str]:
    """检查 tests/ 下是否新增 skip / xfail。

    采用 AST 级判定（见 :func:`_skip_xfail_markers`）：只捕获真正的
    装饰器与调用，忽略字符串常量里作为**被测数据**出现的同名字面量。
    这样既不会漏掉真实的跳过行为，也不会误伤守护边界的断言。

    若某个测试文件无法解析为 Python（语法错误），按违规处理并报告位置——
    解析不了的文件不能当作「没有问题」放过。
    """
    pats = stages_cfg.get("gate", {}).get("forbid_patterns", [])
    tests_dir = ROOT / "tests"
    if not tests_dir.is_dir() or not pats:
        return True, "无 tests 目录或未配置禁止模式"

    hits: list[str] = []
    for path in sorted(tests_dir.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            hits.append(f"{path.relative_to(ROOT)}:{exc.lineno}: 无法解析（{exc.msg}）")
            continue
        for found in _skip_xfail_markers(tree):
            hits.append(f"{path.relative_to(ROOT)}:{found}")

    if hits:
        return False, "发现 skip/xfail：" + "; ".join(hits[:5])
    return True, "无新增 skip/xfail"


def build_prompt(stages_cfg: dict, state: dict, guide_text: str, prev: dict | None) -> str:
    """组装下一轮提示词（含上下文包）。"""
    stage = next((s for s in stages_cfg["stages"] if s["id"] == state["current_stage"]), None)
    if stage is None:
        raise SystemExit(f"[ERR] 未知阶段：{state['current_stage']}")

    baseline = state.get("baseline", {})
    deps_desc = ", ".join(stage["deps"]) if stage["deps"] else "无"
    outputs = ", ".join(stage.get("outputs", [])) or "（见指南）"

    lines: list[str] = []
    lines.append(f"[角色] 你是 SDL 项目的实现者。严格遵守单一增量规则。")
    lines.append("")
    lines.append(f"[任务] 完成阶段 {stage['id']}：{stage['name']}。")
    for pl in stage.get("prompt_lines", []):
        # 首行若与标题重复则跳过，避免提示词中同一句出现两次
        if pl.strip().startswith(f"完成阶段 {stage['id']}"):
            continue
        lines.append(pl)
    lines.append("")
    lines.append("[约束]")
    lines.append("- 只允许新建或修改本阶段声明的交付物：" + outputs)
    lines.append("- 不得修改 sdl_m01/ 下任何文件")
    lines.append("- 不得实现后续阶段的内容")
    lines.append("- 使用 Python 3.11+ 标准库，不引入第三方依赖")
    lines.append("- 全部注释与文档使用中文")
    lines.append("- 输出必须是完整可运行的文件内容，不要省略号占位")
    lines.append("- 必须新增或补充 tests/ 下的单元测试覆盖本阶段验收标准")
    lines.append("")
    lines.append("[上下文包 CP-%s]" % stage["id"])
    lines.append(f"1) 仓库路径：{ROOT}")
    lines.append(
        "2) 当前基线：%s；%s"
        % (baseline.get("unittest", "未运行"), baseline.get("demo", "未运行"))
    )
    lines.append(f"3) 相关契约：{stages_cfg.get('guide', '')} 的 {stage.get('guide_section', '')}；M1 契约见 IMPLEMENTATION_CONTRACT.md")
    lines.append(f"4) 已有实现：前置阶段 {deps_desc}；本阶段将新建 {outputs}")
    lines.append(f"5) 本阶段增量卡：见上方 [任务] 与验收标准")
    lines.append("6) 禁止事项：不得注入或读取任何角色令牌、evidence 数据库内容或受限质量报告")
    if prev:
        lines.append("")
        lines.append("[上一轮交接块]")
        lines.append(f"- 落地文件：{', '.join(prev.get('changed_files', [])) or '（无记录）'}")
        lines.append(f"- 未决问题：{prev.get('open_issues', '（无）')}")
        lines.append(f"- 已知限制：{prev.get('limits', '（无）')}")
    lines.append("")
    lines.append("[交付格式]")
    lines.append("1. 变更文件清单及每个文件的完整内容")
    lines.append("2. 本阶段验收标准的逐条自检结果")
    lines.append("3. 已运行的验证命令与原始输出")
    lines.append("4. 不确定项与已知限制")
    lines.append("5. 交接块")
    lines.append("")
    lines.append(
        "详细阶段定义与规则见 %s（当前阶段见 %s）。"
        % (stages_cfg.get("guide", ""), stage.get("guide_section", ""))
    )
    return "\n".join(lines)


def write_current_prompt(stages_cfg: dict, state: dict) -> Path | None:
    """把「当前阶段」的工单写入 current-prompt.txt。

    与 next-prompt.txt 的区别：next-prompt.txt 只在阶段推进瞬间写入、描述的是下一阶段；
    本文件在任何判定下都反映 state 里的当前阶段，是执行方（定时任务的 agent）的唯一权威工单。
    blocked 阶段不写工单（应等人工处理），返回 None。
    """
    if state.get("stage_status") == "blocked":
        return None
    cur = state.get("current_stage")
    stage = next((s for s in stages_cfg["stages"] if s["id"] == cur), None)
    if stage is None:
        return None
    guide_path = ROOT / stages_cfg.get("guide", "")
    guide_text = guide_path.read_text(encoding="utf-8") if guide_path.is_file() else ""
    prompt = build_prompt(stages_cfg, state, guide_text, state.get("prev_handoff"))
    CURRENT_PROMPT_PATH.write_text(prompt, encoding="utf-8")
    return CURRENT_PROMPT_PATH


def _pid_alive(pid: int) -> bool:
    """判断进程是否存活（跨平台，仅用标准库）。

    为什么需要：定时任务取得租约后若**会话提前结束**（实现未完成就退出），
    租约文件会留在磁盘上，把该阶段一直阻塞到 TTL 到期（默认 55 分钟）。
    实测已遇到这种「孤儿租约」——持有者进程早已不存在，但后续实例仍被
    拒之门外，白白空转近一小时。记录持有者 PID 后可即时发现持有者已死。

    Windows 用 ``OpenProcess`` 查询（需 ``ctypes``）；POSIX 用 ``os.kill(pid, 0)``。
    查不出来时（API 不可用、权限不足）保守返回 ``True``——宁可让 TTL 兜底，
    也不要误判存活进程为已死而造成两个实例同时实现。
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True  # 无法判定 → 保守视为存活
    try:
        if os.name == "nt":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False  # 打不开句柄通常意味着进程已退出
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == STILL_ACTIVE
                return True  # 查不到退出码 → 保守视为存活
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但无权限查询
    except Exception:  # noqa: BLE001 - 任何异常都保守视为存活
        return True


def acquire_work_lease(stages_cfg: dict, stage_id: str) -> tuple[bool, str]:
    """尝试获取「实现当前阶段」的工作租约。返回 (是否获得, 说明)。

    为什么需要：两个错峰定时任务可能在同一分钟内被触发，若都去实现同一增量，
    会互相覆盖交付物。租约保证同一时刻只有一个实例在执行实现。

    租约失效条件（任一满足即可抢占）：

    1. 阶段不同（旧阶段的租约对本阶段无效）；
    2. 超过 TTL（默认 55 分钟），兜底防止永久死锁；
    3. **持有者进程已不存在**——即时回收「孤儿租约」。这一条很关键：
       定时任务的会话可能在实现完成前就结束，留下一个无人持有的租约文件，
       否则该阶段会白白空转到 TTL 到期。

    租约在阶段推进时由脚本自动清除。
    """
    ttl = int(stages_cfg.get("thresholds", {}).get("lease_minutes", 55)) * 60
    now = datetime.now(CST).timestamp()
    if WORKER_LOCK.exists():
        try:
            info = json.loads(WORKER_LOCK.read_text(encoding="utf-8-sig"))
        except Exception:  # noqa: BLE001
            info = {}
        held_stage = info.get("stage")
        held_at = float(info.get("at_epoch", 0))
        held_pid = info.get("pid")
        age = now - held_at
        if held_stage == stage_id and age < ttl:
            if held_pid is not None and not _pid_alive(held_pid):
                # 孤儿租约：持有者已退出却未释放，立即回收。
                pass
            else:
                mins = int(age // 60)
                return False, (
                    f"另一实例正持有 {stage_id} 的工作租约"
                    f"（已 {mins} 分钟，上限 {ttl // 60} 分钟）"
                )
        # 阶段不同 / 已超时 / 孤儿 → 视为失效，允许抢占
    WORKER_LOCK.write_text(
        json.dumps(
            {"stage": stage_id, "at": now_iso(), "at_epoch": now, "pid": os.getpid()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return True, "已获得工作租约"


def release_work_lease() -> None:
    """释放工作租约（阶段推进后由脚本调用）。"""
    try:
        WORKER_LOCK.unlink(missing_ok=True)
    except OSError:
        pass


def write_report(payload: dict) -> None:
    L = payload
    md = [
        f"# DS 阶段循环 · 本轮报告",
        "",
        f"- 时间：{L.get('checked_at', '')}",
        f"- 判定：**{L.get('verdict', '')}**",
        f"- 当前阶段：{L.get('stage_id', '')} {L.get('stage_name', '')}",
        f"- 连续未变更：{L.get('no_change_count', 0)}",
        "",
        "## 依据",
        "",
    ]
    for r in L.get("reasons", []):
        md.append(f"- {r}")
    if L.get("gate"):
        md.append("")
        md.append("## 门禁")
        md.append("")
        for k, v in L["gate"].items():
            mark = "PASS" if v["ok"] else "FAIL"
            md.append(f"- **{mark}** {k}：{v['detail'].splitlines()[0] if v['detail'] else ''}")
    if L.get("action"):
        md.append("")
        md.append("## 本轮动作")
        md.append("")
        md.append(L["action"])
    if L.get("next_prompt_path"):
        md.append("")
        md.append(f"## 下一轮提示词")
        md.append("")
        md.append(f"已写入 `{L['next_prompt_path']}`（阶段 {L.get('next_stage_id', '')}）。")
    if L.get("current_prompt_path") and L.get("verdict") != VERDICT_UPDATED:
        md.append("")
        md.append("## 当前阶段工单")
        md.append("")
        md.append(f"已写入 `{L['current_prompt_path']}`，可直接交给实现方执行。")
    md.append("")
    REPORT_PATH.write_text("\n".join(md), encoding="utf-8")


def do_dispatch(stages_cfg: dict, prompt: str, stage_id: str) -> dict:
    """以 headless 方式调用 WorkBuddy CLI 发起下一轮。返回结果摘要。"""
    d = stages_cfg.get("dispatch", {})
    if not d.get("enabled"):
        return {"dispatched": False, "detail": "dispatch 未启用（配置中 enabled=false）"}
    node, cli = d.get("node"), d.get("cli")
    if not node or not Path(node).is_file():
        return {"dispatched": False, "detail": f"node 不存在：{node}"}
    if not cli or not Path(cli).is_file():
        return {"dispatched": False, "detail": f"CLI 不存在：{cli}"}

    DISPATCH_LOG.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    out_path = DISPATCH_LOG / f"{stamp}-{stage_id}.json"

    cmd = [node, cli, "-p", prompt, "--model", d.get("model", "deepseek-v41-flash"),
           "--max-turns", str(d.get("max_turns", 40)), "--output-format", "json"]
    cmd.extend(d.get("extra_args", []))

    env = dict(os.environ)
    env["CODEBUDDY_FORCE_HEADLESS_BUNDLE"] = "1"

    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT), capture_output=True, text=True,
            timeout=int(d.get("timeout_seconds", 5400)), env=env,
        )
    except subprocess.TimeoutExpired:
        return {"dispatched": True, "ok": False, "detail": "dispatch 超时", "output": str(out_path)}
    except Exception as exc:  # noqa: BLE001
        return {"dispatched": False, "detail": f"dispatch 启动失败：{exc}"}

    out_path.write_text(
        json.dumps(
            {"stage": stage_id, "at": now_iso(), "returncode": proc.returncode,
             "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-2000:]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return {
        "dispatched": True,
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "output": str(out_path),
        "detail": "已发起下一轮" if proc.returncode == 0 else f"CLI 退出码 {proc.returncode}",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="SDL 阶段循环调度器")
    ap.add_argument("--dispatch", action="store_true", help="验收通过后自动发起下一轮")
    ap.add_argument("--dry-run", action="store_true", help="只检测与报告，不写状态、不派发")
    ap.add_argument("--force-stage", help="强制把当前阶段设为指定编号（如 P01）")
    ap.add_argument("--force-run", action="store_true", help="忽略重叠去重，强制执行一次检查")
    ap.add_argument("--work-order", action="store_true",
                    help="输出「是否该我实现 + 当前阶段工单」，供定时任务的执行环节使用")
    ap.add_argument("--release-lease", action="store_true", help="释放工作租约")
    ap.add_argument("--reset", action="store_true", help="重置状态并记录基线指纹")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出报告")
    args = ap.parse_args()

    stages_cfg = load_json(STAGES_PATH)
    stages = stages_cfg["stages"]
    ids = [s["id"] for s in stages]

    config_warns = validate_config(stages_cfg)
    if config_warns:
        print("[配置警示]")
        for w in config_warns:
            print(f"  - {w}")

    just_initialized = False
    reset_or_init = args.reset or not STATE_PATH.exists()
    if reset_or_init:
        if not args.dry_run:
            save_json(STATE_PATH, init_state(stages[0]))
        if args.reset and not args.dry_run:
            print(f"[OK] 状态已重置，当前阶段 {stages[0]['id']} {stages[0]['name']}，基线指纹已记录。")
            return 0
        just_initialized = True

    if STATE_PATH.exists():
        state = load_json(STATE_PATH)
    else:
        # dry-run 且无状态文件：用内存态，不落盘
        state = init_state(stages[0])

    if args.force_stage:
        sid = args.force_stage.upper()
        if sid not in ids:
            raise SystemExit(f"[ERR] 未知阶段：{sid}")
        stage = next(s for s in stages if s["id"] == sid)
        state["current_stage"] = sid
        state["stage_status"] = "in_progress"
        state["no_change_count"] = 0
        state["blocked_reason"] = None
        state["baseline_epoch"] = datetime.now(CST).timestamp()
        state["output_fingerprints"] = snapshot_outputs(stage)
        state["test_fingerprints"] = snapshot_tests(stage)
        state["outputs_snapshot_epoch"] = {o: file_mtime(ROOT / o) for o in stage.get("outputs", [])}
        state["last_check"] = now_iso()
        save_json(STATE_PATH, state)
        p = write_current_prompt(stages_cfg, state)
        print(f"[OK] 当前阶段已设为 {sid} {stage['name']}，基线指纹已记录。")
        print(f"     工单已写入 {p}")
        return 0

    cur = state["current_stage"]
    idx = ids.index(cur)
    stage = stages[idx]
    thresholds = stages_cfg.get("thresholds", {})

    if args.release_lease:
        release_work_lease()
        print("[OK] 工作租约已释放。")
        return 0

    # --work-order：定时任务的「执行环节」入口。
    # 输出当前阶段工单，并告知本次实例是否获得实现权（互斥租约）。
    if args.work_order:
        if state.get("stage_status") == "blocked":
            print(f"[BLOCKED] {cur} 处于 blocked：{state.get('blocked_reason')}")
            print("           不生成工单，等待人工处理。")
            return 0
        if state.get("stage_status") == "done":
            print("[DONE] 全部阶段已完成，无工单。")
            return 0
        p = write_current_prompt(stages_cfg, state)
        ok, why = acquire_work_lease(stages_cfg, cur)
        if not ok:
            print(f"[LEASE-DENIED] {why}")
            print(f"               本实例只做检测与上报，不实现 {cur}。")
            return 0
        print(f"[WORK-ORDER] 阶段 {cur} {stage['name']}｜{why}")
        print(f"             待交付物：{', '.join(stage.get('outputs', []))}")
        print(f"             工单文件：{p}")
        print("")
        print("--- 以下为完整工单，请据此实现 ---")
        print(p.read_text(encoding="utf-8") if p else "")
        return 0

    if state.get("stage_status") == "blocked":
        payload = {
            "checked_at": now_iso(), "verdict": VERDICT_BLOCKED,
            "stage_id": cur, "stage_name": stage["name"],
            "no_change_count": state.get("no_change_count", 0),
            "reasons": [f"阶段处于 blocked：{state.get('blocked_reason')}"],
            "action": "不做任何推进。请人工处理后运行 --reset 或 --force-stage。",
        }
        if not args.dry_run:
            write_report(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
              f"[BLOCKED] {cur} {stage['name']}：{state.get('blocked_reason')}")
        return 0

    if state.get("stage_status") == "done":
        # 终态短路：全部阶段已完成，无需再跑门禁（否则会把已交付物
        # 反复判为「新更新」并每次重跑全量测试，既不幂等也误导报告）。
        payload = {
            "checked_at": now_iso(), "verdict": VERDICT_DONE,
            "stage_id": cur, "stage_name": stage["name"],
            "no_change_count": 0,
            "reasons": ["全部阶段已完成"],
            "action": "全部阶段已完成（P16 已通过）。不再调度后续轮次。",
        }
        if not args.dry_run:
            write_report(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
              "[DONE] 全部阶段已完成。")
        return 0

    # 重叠去重：另一实例刚执行过
    last = state.get("last_check")
    overlap_min = thresholds.get("overlap_minutes", 10)
    if last:
        try:
            delta = (datetime.now(CST) - datetime.fromisoformat(last)).total_seconds() / 60.0
            if 0 <= delta < overlap_min and not args.force_run and not just_initialized:
                payload = {
                    "checked_at": now_iso(), "verdict": VERDICT_OVERLAP,
                    "stage_id": cur, "stage_name": stage["name"],
                    "no_change_count": state.get("no_change_count", 0),
                    "reasons": [f"距上次检查仅 {delta:.1f} 分钟，判定为另一实例刚执行"],
                    "action": "跳过，避免重复推进阶段（如需强制执行请加 --force-run）。",
                }
                print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
                      f"[SKIP] 与另一实例执行时间重叠（{delta:.1f} 分钟），跳过。")
                return 0
        except ValueError:
            pass

    updated, reasons = detect_update(stage, state)
    # 说明：基线指纹在建立基线时即固定（见 init_state / 推进分支），
    # 因此“已存在的交付物其内容变化”会被正确识别为一次版本更新，无需再比较 mtime。

    # 未更新
    if not updated:
        n = state.get("no_change_count", 0) + 1
        state["no_change_count"] = n
        state["last_check"] = now_iso()
        warn = thresholds.get("warn_no_change", 4)
        blk = thresholds.get("block_no_change", 8)
        action = "未检测到版本更新，不推进阶段。"
        if n >= blk:
            state["stage_status"] = "blocked"
            state["blocked_reason"] = f"连续 {n} 次检查未检测到变更"
            action = f"连续 {n} 次无变更，已置为 blocked，等待人工介入。"
        elif n >= warn:
            action = f"连续 {n} 次无变更，该阶段可能已停滞，建议人工检查。"
        if not args.dry_run:
            save_json(STATE_PATH, state)
            write_current_prompt(stages_cfg, state)
        payload = {
            "checked_at": now_iso(), "verdict": VERDICT_NOCHANGE,
            "stage_id": cur, "stage_name": stage["name"],
            "no_change_count": n, "reasons": reasons, "action": action,
            "current_prompt_path": str(CURRENT_PROMPT_PATH),
        }
        if not args.dry_run:
            write_report(payload)
            append_log({"at": payload["checked_at"], "verdict": VERDICT_NOCHANGE,
                        "stage": cur, "no_change_count": n})
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
              f"[NOCHANGE] {cur} {stage['name']} 第 {n} 次无变更。{action}")
        return 0

    # 已更新 → 门禁
    ok_tests, tests_detail, new_failures = run_unittest(stages_cfg)
    missing = [o for o in stage.get("outputs", []) if not (ROOT / o).is_file()]
    ok_files = not missing
    ok_frozen, frozen_detail = check_frozen(
        stages_cfg.get("frozen_paths", []), state.get("baseline_epoch", 0)
    )
    ok_pat, pat_detail = check_forbidden_patterns(stages_cfg)
    ok_stests, stests_detail = check_stage_tests(stages_cfg, stage, state)

    gate = {
        "单元测试全绿": {"ok": ok_tests, "detail": tests_detail},
        "交付物齐全": {"ok": ok_files, "detail": "缺失：" + ", ".join(missing) if missing else "全部存在"},
        "阶段测试补充": {"ok": ok_stests, "detail": stests_detail},
        "冻结目录保护": {"ok": ok_frozen, "detail": frozen_detail},
        "无新增 skip/xfail": {"ok": ok_pat, "detail": pat_detail},
    }
    all_ok = all(v["ok"] for v in gate.values())

    state["last_check"] = now_iso()
    state["baseline"]["unittest"] = "通过" if ok_tests else "失败"

    if not all_ok:
        hard_fail = (not ok_tests) or (not ok_frozen) or (not ok_pat)
        if hard_fail:
            state["stage_status"] = "blocked"
            # 阻塞原因必须带明细，避免出现「冻结目录未改动」这类肯定式措辞造成歧义
            state["blocked_reason"] = "；".join(
                f"{k}（{gate[k]['detail'].splitlines()[0] if gate[k]['detail'] else ''}）"
                for k, v in gate.items() if not v["ok"]
            )
        action = "门禁未通过，停在原地等待人工处理。"
        if not args.dry_run:
            save_json(STATE_PATH, state)
            write_current_prompt(stages_cfg, state)
        payload = {
            "checked_at": now_iso(), "verdict": VERDICT_BLOCKED if hard_fail else VERDICT_NOCHANGE,
            "stage_id": cur, "stage_name": stage["name"],
            "no_change_count": state.get("no_change_count", 0),
            "reasons": reasons, "gate": gate, "action": action,
            "current_prompt_path": str(CURRENT_PROMPT_PATH),
        }
        if not args.dry_run:
            write_report(payload)
            append_log({"at": payload["checked_at"], "verdict": payload["verdict"], "stage": cur})
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
              f"[GATE-FAIL] {cur}：{action}")
        return 1

    # 门禁通过 → 推进
    state["history"].append({
        "stage": cur, "name": stage["name"], "status": "done", "at": now_iso(),
    })
    if idx + 1 >= len(stages):
        state["stage_status"] = "done"
        state["current_stage"] = cur
        # 终态也回写指纹：否则 state 永远停留在 MISSING，下次运行会把已交付物
        # 当成「新更新」，反复重跑全量测试并误报发生过更新。
        state["output_fingerprints"] = snapshot_outputs(stage)
        state["test_fingerprints"] = snapshot_tests(stage)
        state["outputs_snapshot_epoch"] = {
            o: file_mtime(ROOT / o) for o in stage.get("outputs", [])
        }
        if not args.dry_run:
            save_json(STATE_PATH, state)
        payload = {
            "checked_at": now_iso(), "verdict": VERDICT_DONE,
            "stage_id": cur, "stage_name": stage["name"],
            "no_change_count": 0,
            "reasons": reasons, "gate": gate,
            "action": "全部阶段已完成（P16 已通过）。不再调度后续轮次。",
        }
        if not args.dry_run:
            write_report(payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
              "[DONE] 全部阶段已完成。")
        return 0

    next_stage = stages[idx + 1]
    state["current_stage"] = next_stage["id"]
    state["stage_status"] = "in_progress"
    state["no_change_count"] = 0
    state["last_stage_update"] = now_iso()
    state["baseline_epoch"] = datetime.now(CST).timestamp()
    state["output_fingerprints"] = snapshot_outputs(next_stage)
    state["test_fingerprints"] = snapshot_tests(next_stage)
    state["outputs_snapshot_epoch"] = {
        o: file_mtime(ROOT / o) for o in next_stage.get("outputs", [])
    }
    state["baseline"]["demo"] = state["baseline"].get("demo", "未运行")

    guide_path = ROOT / stages_cfg.get("guide", "")
    guide_text = guide_path.read_text(encoding="utf-8") if guide_path.is_file() else ""
    prompt = build_prompt(stages_cfg, state, guide_text, state.get("prev_handoff"))
    if not args.dry_run:
        NEXT_PROMPT_PATH.write_text(prompt, encoding="utf-8")

    dispatch_result = {"dispatched": False, "detail": "未请求派发"}
    if args.dispatch and not args.dry_run:
        dispatch_result = do_dispatch(stages_cfg, prompt, next_stage["id"])

    if not args.dry_run:
        save_json(STATE_PATH, state)
        write_current_prompt(stages_cfg, state)
        release_work_lease()
    payload = {
        "checked_at": now_iso(), "verdict": VERDICT_UPDATED,
        "stage_id": cur, "stage_name": stage["name"],
        "no_change_count": 0,
        "reasons": reasons, "gate": gate,
        "next_stage_id": next_stage["id"], "next_stage_name": next_stage["name"],
        "next_prompt_path": str(NEXT_PROMPT_PATH),
        "current_prompt_path": str(CURRENT_PROMPT_PATH),
        "dispatch": dispatch_result,
        "action": f"{cur} 验收通过，已推进至 {next_stage['id']} {next_stage['name']}。",
    }
    if not args.dry_run:
        write_report(payload)
        append_log({"at": payload["checked_at"], "verdict": VERDICT_UPDATED,
                    "stage": cur, "next": next_stage["id"]})
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else
          f"[UPDATED] {cur} 通过，推进至 {next_stage['id']} {next_stage['name']}。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
