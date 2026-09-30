"""Deterministic fixture provider. It never connects to a model or the network."""
import json
import threading
import uuid


FIXED_STATS = '''"""Small statistics helpers."""


def average(numbers):
    """Return the mean; an empty sequence raises ValueError."""
    if not numbers:
        raise ValueError("numbers must not be empty")
    return sum(numbers) / len(numbers)
'''

ENHANCED_STATS = FIXED_STATS + '''

def summarize(numbers):
    """Return count, mean, minimum and maximum for a nonempty sequence."""
    mean = average(numbers)
    return {"count": len(numbers), "average": mean,
            "min": min(numbers), "max": max(numbers)}
'''

FIXED_TESTS = '''import unittest
from stats import average


class StatsTests(unittest.TestCase):
    def test_average(self):
        self.assertEqual(average([1, 2, 3]), 2)

    def test_negative_average(self):
        self.assertEqual(average([-4, 2]), -1)

    def test_empty_average(self):
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            average([])


if __name__ == "__main__":
    unittest.main()
'''

ENHANCED_TESTS = FIXED_TESTS.replace("from stats import average", "from stats import average, summarize").replace(
    '\n\nif __name__', '''
    def test_summary(self):
        self.assertEqual(summarize([1, 2, 3]),
                         {"count": 3, "average": 2, "min": 1, "max": 3})

    def test_singleton_summary(self):
        self.assertEqual(summarize([-2]),
                         {"count": 1, "average": -2, "min": -2, "max": -2})

    def test_empty_summary(self):
        with self.assertRaises(ValueError):
            summarize([])


if __name__''')


def finding(kind):
    fix = kind == "fix"
    return {"id": "A1" if fix else "I1", "kind": kind,
            "title": "明确空序列平均值的行为" if fix else "增加一次调用获取统计摘要的能力",
            "problem": "空序列会触发除零错误，接口缺少明确的输入约定。" if fix else "调用方需要自行组合多个统计操作才能得到数据概览。",
            "evidence": [{"path": "stats.py", "line": 6, "quote": "return sum(numbers) / len(numbers)"}],
            "hypothesis": "" if fix else "若提供统一摘要接口，调用方可以用一次调用替代多个统计操作；真实使用价值待验证。",
            "expected_value": "空输入得到明确 ValueError，正常输入保持兼容。" if fix else "一次调用返回 count、average、min、max。",
            "confidence": 1.0 if fix else 0.8, "impact": 4 if fix else 3,
            "effort": 1 if fix else 2, "risk": "low"}


def requirement(kind):
    fix = kind == "fix"
    return {"id": "R1" if fix else "R2", "kind": kind,
            "title": finding(kind)["title"], "finding_ids": ["A1" if fix else "I1"],
            "problem": finding(kind)["problem"], "outcome": finding(kind)["expected_value"],
            "acceptance_criteria": ["average([]) 抛出 ValueError 并说明输入不能为空", "现有非空序列平均值结果保持不变"] if fix
            else ["summarize 返回 count、average、min、max 四项", "摘要接口支持单元素与负数，空序列抛出 ValueError"],
            "allowed_files": ["stats.py", "tests/test_stats.py"], "dependencies": [] if fix else ["R1"]}


class DemoProvider:
    """Exercise the real tool loop using visibly simulated agent decisions."""
    def __init__(self):
        self._lock = threading.Lock()
        self._calls = 0

    def snapshot(self):
        with self._lock:
            return {"calls": 0, "simulated_calls": self._calls, "input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def respond(self, *, model, instructions, input, tools, max_output_tokens):
        with self._lock:
            self._calls += 1
        data = json.loads(input[0]["content"])
        step = sum(item.get("type") == "function_call" for item in input)
        read = ("read_file", {"path": "stats.py", "start_line": 1, "end_line": 100})
        if "角色 AUDITOR" in instructions or "角色 INNOVATOR" in instructions:
            kind = "fix" if "角色 AUDITOR" in instructions else "innovation"
            sequence = [("list_files", {"path": "."}), read,
                        ("final_result", {"project_summary": "演示项目提供简单统计函数。",
                                          "findings": [finding(kind)],
                                          "limitations": ["离线演示使用固定发现，只用于验证编排流程。"]})]
        elif "角色 PLANNER" in instructions:
            sequence = [("final_result", {"strategy": "先明确边界行为，再增加兼容的摘要功能。",
                                          "requirements": [requirement("fix"), requirement("innovation")], "deferred": []})]
        elif "角色 DEVELOPER" in instructions:
            fix = data["requirement"]["kind"] == "fix"
            sequence = [read,
                        ("write_file", {"path": "stats.py", "content": FIXED_STATS if fix else ENHANCED_STATS}),
                        ("write_file", {"path": "tests/test_stats.py", "content": FIXED_TESTS if fix else ENHANCED_TESTS}),
                        ("final_result", {"summary": "已写入实现与回归测试（演示）。",
                                          "changed_files": ["stats.py", "tests/test_stats.py"], "notes": []})]
        elif "角色 REVIEWER" in instructions:
            sequence = [read, ("final_result", {"approved": True, "summary": "演示审查通过；实际测试由执行器记录。",
                         "criteria": [{"criterion": c, "passed": True, "evidence": "演示候选 stats.py 及 tests/test_stats.py 中的对应实现。"}
                                      for c in data["requirement"]["acceptance_criteria"]], "issues": []})]
        else:
            raise ValueError("离线 provider 不支持此角色")
        if step >= len(sequence):
            raise ValueError("离线演示出现未预期的工具重试，请检查协议")
        name, args = sequence[step]
        return {"status": "completed", "output": [{"type": "function_call", "call_id": "demo_" + uuid.uuid4().hex,
                "name": name, "arguments": json.dumps(args, ensure_ascii=False)}]}
