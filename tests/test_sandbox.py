"""M12 隔离代码执行环境（``sdl_m12/sandbox.py``）的验收测试。

覆盖：

* 边界常量与 AST 自检（本模块不新增算法、不碰 M1、不触网、不碰文件系统）；
* 静态检查：白名单内外、危险名字、下划线属性、禁止语句；
* 执行：正常计算、运行期异常、超时/指令超限、输出截断、返回值 JSON 化；
* **关键不变式**：静态检查未过的代码**绝不执行**（不得有「先试跑」的路径）；
* 越狱尝试清单——每一条都必须被拦下（证明沙箱不是空壳）。
"""

from __future__ import annotations

import ast
import pathlib
import unittest

from sdl_m12 import sandbox as S
from sdl_m12.sandbox import (
    ALLOWED_MODULES, FORBIDDEN_ACCESS_NAMES, FORBIDDEN_NAMES,
    NOT_PROVIDED_BY_P20, STATUS_FAILED, STATUS_OK, STATUS_REJECTED,
    STATUS_TIMEOUT, SandboxInputError, SandboxRun, SandboxVerdict,
    check_code, run_code, self_check,
)

MODULE_PATH = pathlib.Path(S.__file__)
TEST_PATH = pathlib.Path(__file__)


def _module_source() -> str:
    return MODULE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------

class BoundaryTests(unittest.TestCase):
    """机检常量与 AST 自检。"""

    def test_self_check_passes_on_real_source(self):
        report = self_check()
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["defined_other_stages"], ())
        self.assertEqual(report["accessed_forbidden"], ())
        self.assertFalse(report["imports_m01"])

    def test_boundary_flags_are_all_false(self):
        self.assertFalse(S.SANDBOX_ADDS_ALGORITHMS)
        self.assertFalse(S.SANDBOX_GRANTS_EVIDENCE_GRADE)
        self.assertFalse(S.SANDBOX_ACCESSES_M1)
        self.assertFalse(S.SANDBOX_ALLOWS_NETWORK)
        self.assertFalse(S.SANDBOX_ALLOWS_FILESYSTEM)
        self.assertFalse(S.SANDBOX_RUNS_UNCHECKED_CODE)

    def test_source_has_no_m01_import_statement(self):
        """源码里不得出现导入 M1 的**语句**（文本提及不算）。"""
        tree = ast.parse(_module_source())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotEqual(a.name.split(".")[0], "sdl_m01")
            elif isinstance(node, ast.ImportFrom):
                self.assertNotEqual((node.module or "").split(".")[0], "sdl_m01")

    def test_module_does_not_import_network_or_process_modules(self):
        """本模块自身只用标准库纯计算模块，不导入网络/进程/文件模块。"""
        tree = ast.parse(_module_source())
        banned = {"subprocess", "socket", "ctypes", "shutil", "pickle", "mmap"}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    self.assertNotIn(a.name.split(".")[0], banned, a.name)
            elif isinstance(node, ast.ImportFrom):
                self.assertNotIn((node.module or "").split(".")[0], banned)

    def test_self_check_ignores_boundary_declarations(self):
        """禁用名出现在**规则表常量**里，不得被自检判成违规（AST 而非文本匹配）。"""
        report = self_check()
        self.assertNotIn("eval", report["accessed_forbidden"])
        self.assertNotIn("open", report["accessed_forbidden"])

    def test_detector_catches_planted_definition(self):
        planted = "def compute_p_value(x):\n    return 0.05\n"
        tree = ast.parse(planted)
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertIn("compute_p_value", defined & set(NOT_PROVIDED_BY_P20))

    def test_detector_catches_planted_m01_import(self):
        planted = "import sdl_m01\n"
        tree = ast.parse(planted)
        found = any(isinstance(n, ast.Import) and
                    any(a.name.split(".")[0] == "sdl_m01" for a in n.names)
                    for n in ast.walk(tree))
        self.assertTrue(found)


# ---------------------------------------------------------------------------
# 静态检查
# ---------------------------------------------------------------------------

class StaticCheckTests(unittest.TestCase):
    """AST 静态检查的判定行为。"""

    def test_allows_plain_arithmetic(self):
        v = check_code("def main(x):\n    return x * 2 + 1\n")
        self.assertTrue(v.allowed, v.to_dict())

    def test_allows_whitelisted_import(self):
        for mod in ("math", "statistics", "json", "itertools"):
            v = check_code(f"import {mod}\n")
            self.assertTrue(v.allowed, (mod, v.to_dict()))

    def test_rejects_syntax_error(self):
        v = check_code("def main(:\n    pass\n")
        self.assertFalse(v.allowed)
        self.assertEqual(v.violations[0]["rule"], "不可解析")

    def test_rejects_non_string_source(self):
        with self.assertRaises(SandboxInputError):
            check_code(b"print(1)")

    def test_rejects_non_whitelisted_imports(self):
        for mod in ("os", "sys", "subprocess", "socket", "urllib",
                    "importlib", "ctypes", "pickle", "shutil", "pathlib"):
            v = check_code(f"import {mod}\n")
            self.assertFalse(v.allowed, mod)

    def test_rejects_relative_import(self):
        v = check_code("from . import sibling\n")
        self.assertFalse(v.allowed)

    def test_rejects_forbidden_calls(self):
        for expr in ("open('x')", "eval('1')", "exec('x=1')", "compile('1','','exec')",
                     "__import__('os')", "input()", "globals()", "locals()",
                     "vars()", "dir()", "getattr(x,'y')", "setattr(x,'y',1)"):
            v = check_code(f"def main():\n    return {expr}\n")
            self.assertFalse(v.allowed, expr)

    def test_rejects_dunder_attribute_access(self):
        for expr in ("(1).__class__", "''.__class__.__bases__", "x.__dict__",
                     "().__class__.__mro__"):
            v = check_code(f"def main(x):\n    return {expr}\n")
            self.assertFalse(v.allowed, expr)

    def test_rejects_forbidden_statements(self):
        for src in ("global x\n", "def f():\n    nonlocal y\n",
                    "async def f():\n    pass\n", "del x\n"):
            v = check_code(src)
            self.assertFalse(v.allowed, src)

    def test_violations_carry_rule_and_lineno(self):
        v = check_code("import os\n")
        self.assertFalse(v.allowed)
        rule_names = {x["rule"] for x in v.violations}
        self.assertTrue(rule_names)
        for item in v.violations:
            self.assertIn("rule", item)
            self.assertIn("lineno", item)
            self.assertGreaterEqual(item["lineno"], 0)

    def test_verdict_is_serialisable_and_deterministic(self):
        v = check_code("import os\n")
        a = v.canonical_json()
        b = check_code("import os\n").canonical_json()
        self.assertEqual(a, b)
        self.assertIsInstance(SandboxVerdict(allowed=True).to_dict(), dict)


# ---------------------------------------------------------------------------
# 越狱尝试（每条都必须被拦）
# ---------------------------------------------------------------------------

class EscapeAttemptTests(unittest.TestCase):
    """一组典型越狱手法——静态检查必须全部拦下。"""

    ESCAPES = {
        "读文件": "def main():\n    return open('/etc/passwd').read()\n",
        "起进程": "import subprocess\n",
        "建连接": "import socket\n",
        "发请求": "import urllib.request\n",
        "动态导入": "def main():\n    return __import__('os').system('id')\n",
        "动态求值": "def main():\n    return eval('1+1')\n",
        "动态编译": "def main():\n    return compile('x=1', '<s>', 'exec')\n",
        "反射穿越": "def main(o):\n    return o.__class__.__mro__[1].__subclasses__()\n",
        "拿内置": "def main():\n    return __builtins__\n",
        "拿加载器": "def main():\n    return __loader__\n",
        "拿规格": "def main():\n    return __spec__\n",
        "拿包名": "def main():\n    return __package__\n",
        "全局表": "def main():\n    return globals()\n",
        "裸名字引用": "def main():\n    return os\n",
        "裸名字赋值": "x = sys\n",
        "相对导入": "from .. import parent\n",
        "c 扩展": "import ctypes\n",
        "序列化": "import pickle\n",
        "文件系统": "import pathlib\n",
        "异步逃逸": "async def main():\n    pass\n",
        "生成器挂起": "def main():\n    yield 1\n",
        "异常组": "def main():\n    try:\n        pass\n    except* ValueError:\n        pass\n",
    }

    def test_all_escape_attempts_are_rejected(self):
        for label, src in self.ESCAPES.items():
            with self.subTest(label=label):
                v = check_code(src)
                self.assertFalse(v.allowed, f"{label} 竟被放行！{v.to_dict()}")

    def test_rejected_escape_never_executes(self):
        """**关键不变式**：被拒的代码绝不能产生副作用。"""
        marker = pathlib.Path(MODULE_PATH.parent.parent / ".escape-marker")
        if marker.exists():
            marker.unlink()
        src = (
            "def main():\n"
            f"    open({str(marker)!r}, 'w').write('pwned')\n"
            "    return 1\n"
        )
        run = run_code(src, entry="main")
        self.assertEqual(run.status, STATUS_REJECTED)
        self.assertFalse(marker.exists(), "被拒代码竟留下了副作用！")


# ---------------------------------------------------------------------------
# 执行
# ---------------------------------------------------------------------------

class ExecutionTests(unittest.TestCase):
    """受限运行时的执行行为。"""

    def test_runs_pure_computation(self):
        src = (
            "def main(values):\n"
            "    total = sum(values)\n"
            "    return {'total': total, 'n': len(values)}\n"
        )
        run = run_code(src, entry="main", inputs={"values": [1, 2, 3, 4]})
        self.assertEqual(run.status, STATUS_OK, run.to_dict())
        self.assertEqual(run.value, {"total": 10, "n": 4})

    def test_statistics_module_is_usable(self):
        src = (
            "import statistics\n"
            "def main(values):\n"
            "    return {'mean': statistics.mean(values)}\n"
        )
        run = run_code(src, entry="main", inputs={"values": [2, 4, 6]})
        self.assertEqual(run.status, STATUS_OK)
        self.assertAlmostEqual(run.value["mean"], 4.0)

    def test_linear_regression_on_synthetic_data(self):
        """一个真实的分析计算：最小二乘斜率应恢复为 2。"""
        src = (
            "def main(xs, ys):\n"
            "    n = len(xs)\n"
            "    mx = sum(xs)/n\n"
            "    my = sum(ys)/n\n"
            "    sxx = sum((x-mx)**2 for x in xs)\n"
            "    sxy = sum((x-mx)*(y-my) for x, y in zip(xs, ys))\n"
            "    slope = sxy/sxx\n"
            "    return {'slope': slope}\n"
        )
        run = run_code(src, entry="main",
                       inputs={"xs": [1, 2, 3, 4, 5], "ys": [2, 4, 6, 8, 10]})
        self.assertEqual(run.status, STATUS_OK, run.to_dict())
        self.assertAlmostEqual(run.value["slope"], 2.0, places=9)

    def test_runtime_error_is_reported_not_raised(self):
        run = run_code("def main():\n    return 1/0\n", entry="main")
        self.assertEqual(run.status, STATUS_FAILED)
        self.assertIn("ZeroDivisionError", run.error or "")

    def test_infinite_loop_times_out(self):
        src = "def main():\n    while True:\n        pass\n"
        run = run_code(src, entry="main",
                       limits={"timeout_seconds": 1.0, "max_instructions": 20_000})
        self.assertEqual(run.status, STATUS_TIMEOUT)
        self.assertLess(run.duration_ms, 3000.0)

    def test_wall_clock_timeout_trips_without_instruction_cap(self):
        """**纯墙钟超时**路径：指令上限拉到极高，只有截止时间能拦下它。

        这一条与上面那条是**两条独立防线**——上面那条靠指令计数先触发，
        删掉墙钟判定它也照样通过，从而掩盖墙钟分支从未生效。这里刻意把
        ``max_instructions`` 设得远大于循环在超时前能执行的指令数，迫使
        ``status`` 只能由墙钟判定产生。
        """
        src = "def main():\n    while True:\n        pass\n"
        run = run_code(src, entry="main",
                       limits={"timeout_seconds": 0.3, "max_instructions": 10**12})
        self.assertEqual(run.status, STATUS_TIMEOUT, run.to_dict())
        self.assertLess(run.duration_ms, 3000.0)

    def test_recursion_is_bounded(self):
        src = "def main():\n    def f(n):\n        return f(n+1)\n    return f(0)\n"
        run = run_code(src, entry="main",
                       limits={"timeout_seconds": 2.0, "max_instructions": 200_000})
        self.assertIn(run.status, (STATUS_FAILED, STATUS_TIMEOUT))

    def test_output_is_captured_and_truncated(self):
        src = "def main():\n    print('x' * 100000)\n    return 1\n"
        run = run_code(src, entry="main", limits={"max_output": 100})
        self.assertEqual(run.status, STATUS_OK)
        self.assertLessEqual(len(run.stdout), 200)
        self.assertIn("截断", run.stdout)

    def test_return_value_is_json_serialisable(self):
        src = "def main():\n    return {1, 2, 3}\n"
        run = run_code(src, entry="main")
        self.assertEqual(run.status, STATUS_OK)
        import json
        json.dumps(run.value)  # 必须可序列化

    def test_non_serialisable_objects_are_described(self):
        src = "def main():\n    return lambda x: x\n"
        run = run_code(src, entry="main")
        self.assertEqual(run.status, STATUS_OK)
        self.assertIsInstance(run.value, str)

    def test_nan_and_inf_do_not_break_serialisation(self):
        src = "def main():\n    return {'a': float('nan'), 'b': float('inf')}\n"
        run = run_code(src, entry="main")
        self.assertEqual(run.status, STATUS_OK)
        import json
        json.dumps(run.value)

    def test_entry_absent_runs_as_script(self):
        run = run_code("x = 1 + 1\n", entry="main")
        self.assertEqual(run.status, STATUS_OK)
        self.assertIsNone(run.value)

    def test_run_result_has_verdict_and_digest(self):
        run = run_code("def main():\n    return 1\n", entry="main")
        self.assertTrue(run.verdict.allowed)
        self.assertEqual(len(run.content_digest()), 64)

    def test_inputs_must_be_mapping(self):
        with self.assertRaises(SandboxInputError):
            run_code("def main():\n    return 1\n", entry="main", inputs=[1, 2])

    def test_consecutive_runs_are_independent(self):
        """两次执行不得互相污染命名空间（每次都要新建 globals）。

        探针不用 ``dir()``（它在禁用表里、会被静态检查正确拦下）。改用
        **裸名字引用**：第一段定义 ``secret``，第二段直接引用 ``secret``。
        若命名空间被复用，第二段会读到 42；正规行为应是 ``NameError``——
        即每次执行都是全新的全局命名空间。
        """
        a = run_code("secret = 42\ndef main():\n    return 1\n", entry="main")
        self.assertEqual(a.status, STATUS_OK)
        b = run_code("def main():\n    return secret\n", entry="main")
        self.assertEqual(b.status, STATUS_FAILED, b.to_dict())
        self.assertIn("NameError", b.error or "")


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------

class ResultObjectTests(unittest.TestCase):

    def test_run_to_dict_roundtrip(self):
        run = run_code("def main():\n    return 1\n", entry="main")
        d = run.to_dict()
        self.assertEqual(d["status"], STATUS_OK)
        self.assertIn("verdict", d)
        self.assertIn("value", d)

    def test_run_canonical_json_is_stable(self):
        a = run_code("def main():\n    return 1\n", entry="main").canonical_json()
        b = run_code("def main():\n    return 1\n", entry="main").canonical_json()
        self.assertEqual(a, b)

    def test_content_digest_excludes_timing(self):
        """内容指纹必须只反映**算出了什么**，不含墙钟耗时。"""
        a = run_code("def main():\n    return 1\n", entry="main")
        b = run_code("def main():\n    return 1\n", entry="main")
        self.assertEqual(a.content_digest(), b.content_digest())
        self.assertNotIn("duration_ms", a.canonical_json())
        # 结果不同则指纹必须不同（防止指纹退化成常数）
        c = run_code("def main():\n    return 2\n", entry="main")
        self.assertNotEqual(a.content_digest(), c.content_digest())

    def test_verdict_allowed_true_serialises(self):
        v = SandboxVerdict(allowed=True, checked_rules=("r1",))
        self.assertEqual(v.to_dict()["allowed"], True)


# ---------------------------------------------------------------------------
# 双向自检（防空洞门禁）
# ---------------------------------------------------------------------------

class SelfCheckTwoWayTests(unittest.TestCase):
    """两侧都测：干扰项不触发，真实违规必被抓。"""

    def test_planted_definition_is_caught(self):
        src = "def compute_p_value(x):\n    return 0.05\n"
        self.assertIn("compute_p_value", _planted_check(src, "def"))

    def test_planted_access_is_caught(self):
        src = "sqlite3.connect('x')\n"
        found = _planted_check(src, "access")
        self.assertIn("sqlite3", found)

    def test_boundary_declaration_is_not_flagged(self):
        src = 'NOTE = "本层不做 compute_p_value 与 grade_for"\n'
        self.assertEqual(_planted_check(src, "def"), set())


def _planted_check(source: str, mode: str) -> set[str]:
    """复刻 self_check 的判定逻辑，供「植入违规」用例使用。"""
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
    if mode == "def":
        return defined & set(NOT_PROVIDED_BY_P20)
    return (accessed & set(FORBIDDEN_ACCESS_NAMES)) - local_bindings


if __name__ == "__main__":
    unittest.main()
