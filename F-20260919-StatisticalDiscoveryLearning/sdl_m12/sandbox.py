"""M12 · 隔离代码执行环境（受检查的 LLM 代码沙箱）。

本模块回答的问题是：《SDL算法框架说明》§2 末段写「**LLM 生成的代码须经检查并在
隔离执行环境运行**」。这句话有两个硬要求，缺一不可——「须经检查」与「隔离环境」。
M12 就是这句话的落地：它接收一段由 LLM 提交的**分析代码**，先做**AST 静态检查**，
只有完全通过检查的代码才被允许在**受限运行时**里执行；任何一项检查不过，代码
**一律不执行**（不是「先跑再看」）。

边界（可机检，见 :func:`self_check`）：

① **只做纯计算，不碰外部世界。**
   禁止导入 ``os`` / ``sys`` / ``subprocess`` / ``socket`` / ``urllib`` / ``importlib`` /
   ``ctypes`` / ``pickle`` / ``shutil`` / ``pathlib`` / ``io`` / ``builtins`` 等；
   禁止调用 ``open`` / ``eval`` / ``exec`` / ``compile`` / ``__import__`` / ``input`` /
   ``breakpoint`` / ``globals`` / ``locals`` / ``vars`` / ``dir`` 等；
   禁止属性访问下划线名（``obj.__class__`` 之类的绕过路径）。

② **不访问 M1。** 本模块**不导入** ``sdl_m01``，也不接受任何 M1 令牌或数据引用。
   它的输入输出都是 JSON 基础类型——需要数据时，由调用方把**已脱敏的数值**传进来。

③ **不新增统计能力、不授予证据等级。** 沙箱只负责「安全地把这段计算跑完」；
   跑出来的数字是不是证据，由 M6/M8 的机械闸门决定，与沙箱无关。

④ **资源受限。** 指令数、墙钟时间、递归深度、输出长度、容器体积均设上限，
   防止 LLM 代码里的死循环或内存炸弹拖垮宿主。

为什么不用子进程/容器做隔离：项目纪律规定 M2–M12 仅依赖 Python 标准库且不在
本层引入进程管理（``subprocess`` 本身就在禁用清单里）。因此这里采用**同进程 + AST
前置检查 + 受限命名空间 + trace 限额**的纵深防御。「静态检查不过就不执行」是第一道
也是最重要的一道闸门——它把绝大多数危险模式挡在执行之前。

注意：同进程沙箱**不是**操作系统级安全域。若未来要运行完全不可信的代码，
仍应在进程/容器级再加一层隔离（这一点与被冻结的 ``sdl_m01`` 的理念一致：它是
本地受信任 SDK 的**逻辑边界**，不是内核级安全边界）。
"""

from __future__ import annotations

import ast
import json
import math
import statistics
import sys
import time
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

# --------------------------------------------------------------------------
# 版本与机检常量
# --------------------------------------------------------------------------

SANDBOX_VERSION = "P20-v1.0"

#: 本模块是否**新增算法/统计能力**。恒为 ``False``——沙箱只执行，不发明方法。
SANDBOX_ADDS_ALGORITHMS = False

#: 本模块是否**授予证据等级**。恒为 ``False``——证据判定属 M6/M8。
SANDBOX_GRANTS_EVIDENCE_GRADE = False

#: 本模块是否**访问 M1 数据或令牌**。恒为 ``False``——沙箱对数据协议一无所知。
SANDBOX_ACCESSES_M1 = False

#: 本模块是否**允许触网**。恒为 ``False``。
SANDBOX_ALLOWS_NETWORK = False

#: 本模块是否**允许文件系统与进程访问**。恒为 ``False``。
SANDBOX_ALLOWS_FILESYSTEM = False

#: 本模块是否**在静态检查未过时仍执行代码**。恒为 ``False``——
#: 「检查不过就不跑」是硬约束，不存在「先试跑」的路径。
SANDBOX_RUNS_UNCHECKED_CODE = False

SANDBOX_BOUNDARY_NOTE = (
    "M12 是隔离代码执行环境：它对 LLM 提交的分析代码先做 AST 静态检查，"
    "只有完全通过的代码才在受限运行时里执行。它不新增算法、不授予证据等级、"
    "不访问 M1 数据与令牌、不触网、不碰文件系统与进程。"
    "静态检查未通过的代码一律不执行——不存在「先跑再看」的路径。"
    "沙箱是本地逻辑边界，不是操作系统级安全域。"
)

# --------------------------------------------------------------------------
# 检查规则
# --------------------------------------------------------------------------

#: 允许导入的纯计算模块白名单（按顶层包名匹配）。
ALLOWED_MODULES: frozenset[str] = frozenset({
    "math", "statistics", "json", "itertools", "functools", "collections",
    "operator", "decimal", "fractions", "random", "cmath", "numbers",
    "bisect", "heapq", "array", "copy", "re", "string", "textwrap", "typing",
})

#: 禁止出现的名字（导入、调用、属性访问一律拦截）。
#: 这些名字本身**就是**风险面——写在这里是**规则定义**，不是本模块在使用它们。
FORBIDDEN_NAMES: frozenset[str] = frozenset({
    # 进程与系统
    "os", "sys", "subprocess", "signal", "multiprocessing", "threading",
    "fcntl", "resource", "pty", "platform", "getpass", "pwd", "grp",
    # 网络
    "socket", "urllib", "http", "ftplib", "smtplib", "telnetlib", "asyncio",
    "ssl", "requests", "httpx", "webbrowser", "xmlrpc", "select",
    # 文件与序列化
    "open", "io", "pathlib", "shutil", "tempfile", "glob", "fileinput",
    "pickle", "shelve", "dbm", "sqlite3", "csv", "tarfile", "zipfile", "gzip",
    # 动态执行与反射
    "eval", "exec", "compile", "__import__", "importlib", "builtins",
    "globals", "locals", "vars", "dir", "getattr", "setattr", "delattr",
    "hasattr", "input", "breakpoint", "exit", "quit", "memoryview",
    # 名字空间探针：直接引用即暴露宿主内部结构（`__builtins__` 是最短的逃逸口）
    "__builtins__", "__loader__", "__spec__", "__package__", "__name__",
    # 原生与底层
    "ctypes", "cffi", "mmap", "gc", "inspect", "code", "codeop", "marshal",
    "dis", "ast", "traceback", "warnings", "logging",
    # 其他危险面
    "help", "object", "type", "super", "classmethod", "staticmethod",
    "property", "__build_class__",
})

#: 禁止的 AST 节点类型（按类名）。
#: 注意：``Import`` / ``ImportFrom`` **不在**此列——导入由 ``ALLOWED_MODULES``
#: 白名单单独管（白名单内的模块要能正常导入，一刀切禁 Import 会把正常代码也拒掉）。
FORBIDDEN_NODE_TYPES: frozenset[str] = frozenset({
    "Global", "Nonlocal", "Delete",
    "AsyncFunctionDef", "Await", "AsyncFor", "AsyncWith", "Yield", "YieldFrom",
    "TryStar",  # except* 语法
})

#: 允许导入的模块 → 实际可用的模块对象（仅白名单内）。
_MODULE_CACHE: dict[str, Any] = {}

#: 运行时仍可用的内置函数（白名单制）。
_SAFE_BUILTINS: Mapping[str, Any] = MappingProxyType({
    "abs": abs, "all": all, "any": any, "bin": bin, "bool": bool,
    "bytearray": bytearray, "bytes": bytes, "chr": chr, "complex": complex,
    "dict": dict, "divmod": divmod, "enumerate": enumerate, "filter": filter,
    "float": float, "format": format, "frozenset": frozenset, "hash": hash,
    "hex": hex, "int": int, "isinstance": isinstance, "issubclass": issubclass,
    "iter": iter, "len": len, "list": list, "map": map, "max": max,
    "min": min, "next": next, "oct": oct, "ord": ord, "pow": pow,
    "print": print, "range": range, "repr": repr, "reversed": reversed,
    "round": round, "set": set, "slice": slice, "sorted": sorted,
    "str": str, "sum": sum, "tuple": tuple, "zip": zip,
    "True": True, "False": False, "None": None,
    "Exception": Exception, "ValueError": ValueError, "TypeError": TypeError,
    "KeyError": KeyError, "IndexError": IndexError, "ZeroDivisionError": ZeroDivisionError,
    "ArithmeticError": ArithmeticError, "OverflowError": OverflowError,
    "StopIteration": StopIteration, "RuntimeError": RuntimeError,
    "AssertionError": AssertionError, "NotImplementedError": NotImplementedError,
})


# --------------------------------------------------------------------------
# 结果对象
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SandboxVerdict:
    """静态检查结论。"""

    allowed: bool
    violations: tuple[Mapping[str, Any], ...] = ()
    checked_rules: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "violations": [dict(v) for v in self.violations],
            "checked_rules": list(self.checked_rules),
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))


@dataclass(frozen=True)
class SandboxRun:
    """隔离执行结果。"""

    status: str
    verdict: SandboxVerdict
    value: Any = None
    stdout: str = ""
    error: str | None = None
    duration_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "verdict": self.verdict.to_dict(),
            "value": self.value,
            "stdout": self.stdout,
            "error": self.error,
            "duration_ms": self.duration_ms,
        }

    def canonical_json(self) -> str:
        """**内容身份**的规范形式——刻意剔除 ``duration_ms``。

        墙钟耗时是运行期测量，不是「这段代码算出了什么」的一部分：同一段代码两次
        执行的数值结果完全相同，但耗时会漂移。若把耗时纳入规范形式，``content_digest``
        将永远不稳定、无法用于比对与去重。因此规范形式只保留**内容**字段。
        （完整字段仍可从 :meth:`to_dict` 取。）
        """
        payload = {k: v for k, v in self.to_dict().items() if k != "duration_ms"}
        return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    def content_digest(self) -> str:
        import hashlib
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


STATUS_OK = "ok"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"


class SandboxError(ValueError):
    """M12 的基类异常。"""


class SandboxInputError(SandboxError):
    """输入类型或取值不合法。"""


#: 越界定义的自检黑名单（本模块**不得**定义这些）。
NOT_PROVIDED_BY_P20: tuple[str, ...] = (
    "compute_p_value", "holm_adjust", "classify_result", "grade_for",
    "bind_confirmation", "consume_confirmation", "record_evaluation",
    "archive_round", "update_knowledge_version", "run_loop",
    "build_default_toolbox", "call_tool",
)

#: 越权访问的自检黑名单（本模块**不得**访问这些）。
FORBIDDEN_ACCESS_NAMES: tuple[str, ...] = (
    "sqlite3", "sdl_m01", "add_confirmation", "consume_confirmation",
    "bind_confirmation", "record_evaluation", "archive_confirmation",
    "release_results", "mark_compromised",
)


# --------------------------------------------------------------------------
# 静态检查
# --------------------------------------------------------------------------

def _iter_target_names(node: ast.AST) -> Iterable[str]:
    """取出赋值/循环目标的绑定名。"""
    if isinstance(node, ast.Name):
        yield node.id
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            yield from _iter_target_names(elt)


def _is_dunder(name: str) -> bool:
    """是否为双下划线名字（``__x__``）。

    双下划线名是宿主内部对象（``__builtins__`` / ``__loader__`` / ``__spec__`` …）
    的通用入口。与其枚举（总有漏网），不如「形如 dunder 即禁」——注意
    ``__all__`` / ``__doc__`` 这类也无正当理由出现在一段纯分析计算里。
    """
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


def check_code(source: str) -> SandboxVerdict:
    """对代码做 AST 静态检查，返回 :class:`SandboxVerdict`。

    只检查、不执行。任何一条规则不过，``allowed`` 即为 ``False``。
    """
    if not isinstance(source, str):
        raise SandboxInputError("source 必须是字符串")

    violations: list[dict[str, Any]] = []
    checked = (
        "不可解析", "禁止模块与名字", "禁止语句类型", "下划线属性访问",
        "危险调用", "顶层结构",
    )

    try:
        tree = ast.parse(source, mode="exec")
    except SyntaxError as exc:
        violations.append({
            "rule": "不可解析",
            "detail": f"{exc.msg}",
            "lineno": exc.lineno or 0,
        })
        return SandboxVerdict(allowed=False, violations=tuple(violations),
                              checked_rules=checked)

    def _add(rule: str, detail: str, lineno: int) -> None:
        violations.append({"rule": rule, "detail": detail, "lineno": lineno})

    # 收集本段代码自身绑定的名字：这些是「本地定义/赋值」，不算越权访问。
    # 与 M10 的 self_check 同理——不剔除会把自己写的临时变量名误判成违规。
    local_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            local_names.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            local_names.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
                    local_names.add(a.arg)
                if args.vararg:
                    local_names.add(args.vararg.arg)
                if args.kwarg:
                    local_names.add(args.kwarg.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                local_names.add((alias.asname or alias.name).split(".")[0])

    for node in ast.walk(tree):
        # --- 禁止的语句类型 ---
        if type(node).__name__ in FORBIDDEN_NODE_TYPES:
            _add("禁止语句类型", f"不允许 {type(node).__name__}",
                 getattr(node, "lineno", 0))

        # --- 导入：只在白名单内 ---
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top not in ALLOWED_MODULES:
                    _add("禁止模块与名字", f"不允许导入 {alias.name}",
                         getattr(node, "lineno", 0))

        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if node.level and node.level > 0:
                _add("禁止模块与名字", "不允许相对导入",
                     getattr(node, "lineno", 0))
            elif top not in ALLOWED_MODULES:
                _add("禁止模块与名字", f"不允许导入 {node.module}",
                     getattr(node, "lineno", 0))

        # --- 名字访问：命中禁用表即拦截（本地绑定名除外） ---
        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_NAMES and node.id not in local_names:
                _add("禁止模块与名字", f"不允许使用名字 {node.id}",
                     getattr(node, "lineno", 0))
            elif _is_dunder(node.id):
                # 通用规则：任何双下划线名字都是宿主内部结构的入口
                # （``__builtins__`` / ``__loader__`` / ``__spec__`` …）。
                # 枚举法总有漏网，这里用「形如 dunder 即禁」兜底。
                _add("禁止模块与名字", f"不允许使用双下划线名字 {node.id}",
                     getattr(node, "lineno", 0))

        # --- 属性访问：下划线名一律禁止（挡 obj.__class__ 等绕过路径） ---
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_"):
                _add("下划线属性访问", f"不允许访问属性 {node.attr}",
                     getattr(node, "lineno", 0))

        # --- 调用：再查一道函数名 ---
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES \
                    and func.id not in local_names:
                _add("危险调用", f"不允许调用 {func.id}",
                     getattr(node, "lineno", 0))

        # --- 顶层结构：不允许裸表达式语句之外的模块级副作用 ---
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            continue

    return SandboxVerdict(
        allowed=not violations,
        violations=tuple(violations),
        checked_rules=checked,
    )


# --------------------------------------------------------------------------
# 受限运行时
# --------------------------------------------------------------------------

class _BudgetExceeded(Exception):
    """指令预算或超时。"""


def _build_globals() -> dict[str, Any]:
    """构造受限全局命名空间：安全内置 + 白名单模块。

    白名单模块在此**预先导入并放入命名空间**，因此不需要在执行期暴露
    ``__import__``——代码里的 ``import statistics`` 由一段受控的桩函数处理
    （见 :func:`_make_importer`），它只认白名单内的名字，其余一律抛
    :class:`ImportError`。这样既让正常 ``import`` 语法可用，又不给任何逃逸口。
    """
    g: dict[str, Any] = {"__builtins__": dict(_SAFE_BUILTINS)}
    for name in ALLOWED_MODULES:
        mod = _MODULE_CACHE.get(name)
        if mod is None:
            try:
                mod = __import__(name)
                _MODULE_CACHE[name] = mod
            except Exception:
                continue
        g[name] = mod
    return g


def _make_importer(globals_ns: Mapping[str, Any]) -> Callable[..., Any]:
    """构造一个**受控的** ``__import__``：只放行白名单模块。

    为什么还是提供 ``__import__``：``exec`` 执行 ``import X`` 语句时，字节码
    会调用当前命名空间的 ``__import__`` 内置。若完全不提供，连 ``import math``
    这样的正常写法都会报 ``ImportError: __import__ not found``。因此给一个
    **只认白名单**的实现：命中白名单返回已预载的模块对象；否则抛 ``ImportError``。
    由于 ``FORBIDDEN_NAMES`` 与静态检查已在更早一道把它拦下，这里是纵深防御的第二道。
    """

    def _restricted_import(name: str, globals_: Any = None, locals_: Any = None,
                           fromlist: Any = (), level: int = 0) -> Any:
        if level and level > 0:
            raise ImportError("不允许相对导入")
        top = name.split(".")[0]
        if top not in ALLOWED_MODULES:
            raise ImportError(f"模块 {name!r} 不在沙箱白名单内")
        mod = globals_ns.get(top)
        if mod is None:
            raise ImportError(f"模块 {name!r} 未能预载")
        return mod

    return _restricted_import


class _Limits:
    """资源限额。"""

    __slots__ = ("max_instructions", "timeout_seconds", "max_output", "max_collection")

    def __init__(self, *, max_instructions: int = 2_000_000,
                 timeout_seconds: float = 5.0, max_output: int = 8_000,
                 max_collection: int = 1_000_000) -> None:
        self.max_instructions = int(max_instructions)
        self.timeout_seconds = float(timeout_seconds)
        self.max_output = int(max_output)
        self.max_collection = int(max_collection)


def _install_tracer(limits: _Limits, deadline: float) -> Callable:
    """安装 trace 钩子，同时承担「指令计数」与「墙钟超时」两件事。

    为什么要用 trace：项目中 M12 不允许使用 ``signal``/``threading``/``subprocess``
    （它们本身就在禁用清单里），而这三者恰是常见的超时实现手段。``sys.settrace``
    是纯 Python 层、无需额外权限、且可在每次行/调用事件上检查预算，正好合用。
    """
    counter = {"n": 0}

    def _trace(frame: Any, event: str, arg: Any):  # noqa: ANN401
        counter["n"] += 1
        if counter["n"] > limits.max_instructions:
            raise _BudgetExceeded("指令数超限")
        if (counter["n"] & 0x3FF) == 0 and time.monotonic() > deadline:
            raise _BudgetExceeded("执行超时")
        return _trace

    return _trace


class _OutputCapture:
    """受限的输出捕获（长度封顶，防止内存膨胀）。"""

    def __init__(self, limit: int) -> None:
        self.limit = int(limit)
        self.chunks: list[str] = []
        self.size = 0
        self.truncated = False

    def write(self, text: str) -> int:
        if self.size >= self.limit:
            self.truncated = True
            return len(text)
        remain = self.limit - self.size
        piece = text[:remain]
        self.chunks.append(piece)
        self.size += len(piece)
        if len(piece) < len(text):
            self.truncated = True
        return len(text)

    def flush(self) -> None:
        return None

    def getvalue(self) -> str:
        out = "".join(self.chunks)
        return out + ("\n…（输出已截断）" if self.truncated else "")


def _jsonable(value: Any, *, max_items: int) -> Any:
    """把返回值压成 JSON 基础类型；不可序列化的降级为类型名描述。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return str(value)
        return value
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        if len(items) > max_items:
            items = items[:max_items]
            return {"__truncated__": True, "items": [_jsonable(v, max_items=max_items) for v in items]}
        return [_jsonable(v, max_items=max_items) for v in items]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(value.items()):
            if i >= max_items:
                out["__truncated__"] = True
                break
            out[str(_jsonable(k, max_items=max_items))] = _jsonable(v, max_items=max_items)
        return out
    return f"<{type(value).__name__}>"


def run_code(source: str, *, entry: str = "main",
             limits: _Limits | Mapping[str, Any] | None = None,
             inputs: Mapping[str, Any] | None = None) -> SandboxRun:
    """在受限运行时里执行代码。

    流程（顺序不可颠倒）：**先静态检查；不过则直接 ``rejected``，绝不执行**。
    检查通过后才编译执行。执行中若超时/超指令数，返回 ``timeout``。

    :param source: 待执行代码。
    :param entry: 若代码定义了该名字且为可调用，则调用它并取其返回值作为 ``value``；
        否则 ``value`` 为 ``None``（代码仅作为脚本执行）。
    :param limits: :class:`_Limits` 或等价映射。
    :param inputs: 传给 ``entry`` 的具名参数（JSON 基础类型）。
    """
    if not isinstance(source, str):
        raise SandboxInputError("source 必须是字符串")

    verdict = check_code(source)
    if not verdict.allowed:
        # 硬约束：检查不过就不执行。这里直接返回，不进入编译/执行分支。
        return SandboxRun(status=STATUS_REJECTED, verdict=verdict)

    if limits is None:
        limits = _Limits()
    elif isinstance(limits, Mapping):
        limits = _Limits(**limits)
    if not isinstance(limits, _Limits):
        raise SandboxInputError("limits 必须是 _Limits 或映射")

    if inputs is not None and not isinstance(inputs, Mapping):
        raise SandboxInputError("inputs 必须是映射或 None")

    try:
        code = compile(source, "<sdl_m12>", "exec")
    except SyntaxError as exc:  # 理论上 check_code 已挡掉，双保险
        v = SandboxVerdict(allowed=False, violations=(
            {"rule": "不可解析", "detail": str(exc), "lineno": exc.lineno or 0},),
            checked_rules=verdict.checked_rules)
        return SandboxRun(status=STATUS_REJECTED, verdict=v)

    capture = _OutputCapture(limits.max_output)
    g = _build_globals()
    g["print"] = lambda *a, **k: capture.write(" ".join(str(x) for x in a) + "\n")
    # 受控导入器：让 `import math` 这类正常写法可用，同时只放行白名单。
    g["__builtins__"]["__import__"] = _make_importer(g)
    started = time.monotonic()
    deadline = started + limits.timeout_seconds
    tracer = _install_tracer(limits, deadline)
    old_trace = sys.gettrace()
    old_limit = sys.getrecursionlimit()
    result_value: Any = None
    error: str | None = None
    status = STATUS_OK

    try:
        sys.settrace(tracer)
        sys.setrecursionlimit(min(old_limit, 400))
        exec(code, g, g)  # noqa: S102 - 这是沙箱的既定职责，且已过静态检查

        fn = g.get(entry)
        if callable(fn):
            call_args = dict(inputs) if inputs else {}
            result_value = fn(**call_args)
    except _BudgetExceeded as exc:
        status = STATUS_TIMEOUT
        error = f"_BudgetExceeded: {exc}"
    except RecursionError:
        status = STATUS_FAILED
        error = "RecursionError: 递归深度超限"
    except Exception as exc:  # noqa: BLE001 - 沙箱必须吞掉一切异常并如实报告
        status = STATUS_FAILED
        error = f"{type(exc).__name__}: {exc}"[:1000]
    finally:
        sys.settrace(old_trace)
        sys.setrecursionlimit(old_limit)

    duration_ms = (time.monotonic() - started) * 1000.0
    if status == STATUS_OK:
        result_value = _jsonable(result_value, max_items=2000)

    return SandboxRun(
        status=status,
        verdict=verdict,
        value=result_value,
        stdout=capture.getvalue(),
        error=error,
        duration_ms=duration_ms,
    )


# --------------------------------------------------------------------------
# 边界自检
# --------------------------------------------------------------------------

def self_check() -> dict[str, Any]:
    """AST 级边界自检：确认本模块未定义越界函数、未访问越权名字。

    与 M10 的 ``self_check`` 同理，采用 AST 而非源码文本匹配：本模块的
    ``FORBIDDEN_NAMES`` 里**必须**写出 ``eval`` / ``open`` 等被禁名字（那是规则定义），
    文本匹配会把规则表本身当成违规。因此只看**真正的定义与访问**，并剔除本地绑定名。
    """
    source = _module_source()
    tree = ast.parse(source)

    defined: set[str] = set()
    accessed: set[str] = set()
    local_bindings: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            local_bindings.add(node.name)
            args = getattr(node, "args", None)
            if args is not None:
                for a in list(args.args) + list(args.posonlyargs) + list(args.kwonlyargs):
                    local_bindings.add(a.arg)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, ast.Store):
                local_bindings.add(node.id)
            else:
                accessed.add(node.id)
        elif isinstance(node, ast.Attribute):
            accessed.add(node.attr)

    defined_violations = sorted(defined & set(NOT_PROVIDED_BY_P20))
    accessed_violations = sorted(
        (accessed & set(FORBIDDEN_ACCESS_NAMES)) - local_bindings
    )

    # 是否导入了 sdl_m01：必须用 **AST 的 import 节点**判定，不能做源码文本匹配。
    # 本模块的 docstring 里就写着「不导入 sdl_m01」这句话——文本匹配会把
    # 边界声明本身当成违规。这是元层自检的经典假阳性（M10 已踩过一次）。
    imports_m01 = any(
        (isinstance(node, ast.Import) and any(
            a.name.split(".")[0] == "sdl_m01" for a in node.names))
        or (isinstance(node, ast.ImportFrom)
            and (node.module or "").split(".")[0] == "sdl_m01")
        for node in ast.walk(tree)
    )

    return {
        "defined_other_stages": tuple(defined_violations),
        "accessed_forbidden": tuple(accessed_violations),
        "imports_m01": imports_m01,
        "adds_algorithms": SANDBOX_ADDS_ALGORITHMS,
        "grants_evidence_grade": SANDBOX_GRANTS_EVIDENCE_GRADE,
        "accesses_m1": SANDBOX_ACCESSES_M1,
        "allows_network": SANDBOX_ALLOWS_NETWORK,
        "allows_filesystem": SANDBOX_ALLOWS_FILESYSTEM,
        "runs_unchecked_code": SANDBOX_RUNS_UNCHECKED_CODE,
        "checked_names": len(NOT_PROVIDED_BY_P20) + len(FORBIDDEN_ACCESS_NAMES),
        "ok": not defined_violations and not accessed_violations and not imports_m01
              and not (SANDBOX_ADDS_ALGORITHMS or SANDBOX_GRANTS_EVIDENCE_GRADE
                       or SANDBOX_ACCESSES_M1 or SANDBOX_ALLOWS_NETWORK
                       or SANDBOX_ALLOWS_FILESYSTEM or SANDBOX_RUNS_UNCHECKED_CODE),
    }


def _module_source() -> str:
    """读取本模块源码（供自检）。"""
    from pathlib import Path
    return Path(__file__).read_text(encoding="utf-8")


__all__ = [
    "SANDBOX_VERSION", "SANDBOX_ADDS_ALGORITHMS", "SANDBOX_GRANTS_EVIDENCE_GRADE",
    "SANDBOX_ACCESSES_M1", "SANDBOX_ALLOWS_NETWORK", "SANDBOX_ALLOWS_FILESYSTEM",
    "SANDBOX_RUNS_UNCHECKED_CODE", "SANDBOX_BOUNDARY_NOTE",
    "ALLOWED_MODULES", "FORBIDDEN_NAMES", "FORBIDDEN_NODE_TYPES",
    "NOT_PROVIDED_BY_P20", "FORBIDDEN_ACCESS_NAMES",
    "SandboxVerdict", "SandboxRun", "SandboxError", "SandboxInputError",
    "STATUS_OK", "STATUS_REJECTED", "STATUS_FAILED", "STATUS_TIMEOUT",
    "check_code", "run_code", "self_check",
]
