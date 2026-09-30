"""Real filesystem tests for workspace boundaries, snapshots and check processes."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from renew.repository import RepoTools, create_snapshot, diff_workspace, run_checks


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "app.py").write_text("print('original')\n", encoding="utf-8")
        self.snapshot = self.base / "snapshot"

    def tearDown(self):
        self.temp.cleanup()

    def test_snapshot_filters_secrets_build_outputs_and_copies_binary(self):
        (self.source / ".env").write_text("OPENAI_API_KEY=secret")
        (self.source / ".env.local").write_text("PASSWORD=secret")
        (self.source / ".env.example").write_text("OPENAI_API_KEY=")
        (self.source / "credentials.json").write_text('{"token":"secret"}')
        (self.source / "private.pem").write_text("secret")
        (self.source / "asset.bin").write_bytes(b"\x00\xff\x01")
        (self.source / "node_modules").mkdir()
        (self.source / "node_modules" / "dependency.js").write_text("x")
        manifest = create_snapshot(self.source, self.snapshot)
        self.assertEqual(set(manifest["files"]), {"app.py", ".env.example", "asset.bin"})
        self.assertGreaterEqual(len(manifest["skipped"]), 5)
        self.assertEqual((self.snapshot / "asset.bin").read_bytes(), b"\x00\xff\x01")
        self.assertFalse(manifest["git_ignore_respected"])

    def test_snapshot_refuses_nested_or_nonempty_destination(self):
        with self.assertRaises(ValueError):
            create_snapshot(self.source, self.source / "nested")
        with self.assertRaises(ValueError):
            create_snapshot(self.source, self.base)
        self.snapshot.mkdir()
        (self.snapshot / "keep.txt").write_text("keep")
        with self.assertRaises(ValueError):
            create_snapshot(self.source, self.snapshot)
        self.assertEqual((self.snapshot / "keep.txt").read_text(), "keep")

    def test_size_limit_fails_before_creating_snapshot(self):
        with patch("renew.repository.MAX_COPY_FILE_BYTES", 1):
            with self.assertRaisesRegex(ValueError, "exceeds snapshot limit"):
                create_snapshot(self.source, self.snapshot)
        self.assertFalse(self.snapshot.exists())
        with patch("renew.repository.MAX_SNAPSHOT_FILES", 0):
            with self.assertRaisesRegex(ValueError, "exceeds snapshot"):
                create_snapshot(self.source, self.snapshot)

    def test_custom_exclusion(self):
        (self.source / "generated").mkdir()
        (self.source / "generated" / "large.txt").write_text("skip")
        manifest = create_snapshot(self.source, self.snapshot, exclude=("generated",))
        self.assertEqual(manifest["files"], ["app.py"])
        self.assertTrue(any(s["reason"] == "user exclusion" for s in manifest["skipped"]))

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_git_ignore_and_tracked_secret_filter(self):
        subprocess.run(["git", "init", str(self.source)], check=True, capture_output=True)
        (self.source / ".gitignore").write_text("ignored.txt\n.env\n")
        (self.source / "ignored.txt").write_text("ignored")
        (self.source / ".env").write_text("OPENAI_API_KEY=secret")
        subprocess.run(["git", "-C", str(self.source), "add", "-f", ".env"], check=True, capture_output=True)
        manifest = create_snapshot(self.source, self.snapshot)
        self.assertTrue(manifest["git_ignore_respected"])
        self.assertIn("app.py", manifest["files"])
        self.assertNotIn("ignored.txt", manifest["files"])
        self.assertFalse((self.snapshot / ".git").exists())
        self.assertFalse((self.snapshot / ".env").exists())

    def test_write_changes_only_snapshot_and_diff_includes_new_file(self):
        create_snapshot(self.source, self.snapshot)
        tools = RepoTools(self.snapshot, writable=True)
        self.assertNotIn("error", tools.dispatch("write_file", {"path": "app.py", "content": "print('improved')\n"}))
        self.assertNotIn("error", tools.dispatch("write_file", {"path": "tests/new.py", "content": "assert True\n"}))
        self.assertEqual((self.source / "app.py").read_text(), "print('original')\n")
        self.assertFalse((self.source / "tests").exists())
        delta = diff_workspace(self.source, self.snapshot)
        self.assertIn("--- a/app.py", delta)
        self.assertIn("+++ b/tests/new.py", delta)
        self.assertIn("new file mode 100644", delta)
        self.assertIn("+assert True", delta)

    def test_readonly_tools_and_strict_schemas(self):
        tools = RepoTools(self.source)
        self.assertNotIn("write_file", [s["name"] for s in tools.specs()])
        self.assertIn("error", tools.dispatch("write_file", {"path": "app.py", "content": "changed"}))
        for spec in tools.specs():
            self.assertTrue(spec["strict"])
            self.assertFalse(spec["parameters"]["additionalProperties"])
            self.assertEqual(set(spec["parameters"]["properties"]), set(spec["parameters"]["required"]))
        self.assertIn("error", tools.dispatch("list_files", {"path": "", "other": 1}))
        self.assertIn("error", tools.dispatch("read_file", {"path": "app.py", "start_line": True, "end_line": 2}))

    def test_path_escape_and_windows_special_paths_are_rejected(self):
        tools = RepoTools(self.source, writable=True)
        invalid = ["../escape.txt", "folder/../../escape", "/absolute.txt", "C:\\absolute.txt", "C:relative.txt",
                   "\\\\server\\share\\file.txt", "file.txt:stream", "CON", "aux.txt", "lpt1", "file. ",
                   ".git/config", "nested/.git/config", ".env", ".env.production", "private.key"]
        for path in invalid:
            with self.subTest(path=path):
                self.assertIn("error", tools.dispatch("write_file", {"path": path, "content": "x"}))
                self.assertIn("error", tools.dispatch("read_file", {"path": path, "start_line": 1, "end_line": 1}))
        self.assertFalse((self.base / "escape.txt").exists())

    def test_links_are_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_text("outside")
        link = self.source / "shortcut"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available")
        tools = RepoTools(self.source, writable=True)
        self.assertIn("error", tools.dispatch("write_file", {"path": "shortcut/secret.txt", "content": "overwrite"}))
        self.assertIn("error", tools.dispatch("read_file", {"path": "shortcut/secret.txt", "start_line": 1, "end_line": 1}))
        create_snapshot(self.source, self.snapshot)
        self.assertFalse((self.snapshot / "shortcut").exists())
        self.assertEqual((outside / "secret.txt").read_text(), "outside")

    def test_atomic_replace_does_not_change_hardlink_target(self):
        outside = self.base / "original.txt"
        outside.write_text("original")
        try:
            os.link(outside, self.source / "hardlink.txt")
        except OSError:
            self.skipTest("hard links unavailable")
        result = RepoTools(self.source, writable=True).dispatch("write_file", {"path": "hardlink.txt", "content": "changed"})
        self.assertNotIn("error", result)
        self.assertEqual(outside.read_text(), "original")

    def test_read_search_and_bounded_results(self):
        (self.source / "text.txt").write_text("alpha\nbeta alpha\ngamma\n", encoding="utf-8")
        (self.source / "image.bin").write_bytes(b"\x00binary")
        tools = RepoTools(self.source)
        result = tools.dispatch("read_file", {"path": "text.txt", "start_line": 2, "end_line": 3})
        self.assertEqual(result["content"], "2: beta alpha\n3: gamma")
        self.assertEqual(result["total_lines"], 3)
        self.assertIn("error", tools.dispatch("read_file", {"path": "image.bin", "start_line": 1, "end_line": 2}))
        self.assertIn("error", tools.dispatch("read_file", {"path": "text.txt", "start_line": 1, "end_line": 201}))
        result = tools.dispatch("search_text", {"query": "alpha"})
        self.assertEqual([m["line"] for m in result["matches"]], [1, 2])
        self.assertEqual(result["skipped_files"], 1)
        with patch("renew.repository.MAX_LIST_RESULTS", 1):
            result = tools.dispatch("list_files", {"path": "."})
            self.assertEqual(len(result["files"]), 1)
            self.assertTrue(result["truncated"])

    def test_diff_handles_deletion_binary_empty_and_missing_newline(self):
        (self.source / "old.txt").write_text("old")
        (self.source / "data.bin").write_bytes(b"\x00old")
        create_snapshot(self.source, self.snapshot)
        (self.snapshot / "old.txt").unlink()
        (self.snapshot / "data.bin").write_bytes(b"\x00new")
        (self.snapshot / "empty.txt").touch()
        delta = diff_workspace(self.source, self.snapshot)
        self.assertIn("deleted file mode", delta)
        self.assertIn("Binary files a/data.bin and b/data.bin differ", delta)
        self.assertIn("diff --git a/empty.txt b/empty.txt", delta)
        self.assertIn("\\ No newline at end of file", delta)

    def test_checks_execute_argv_and_remove_secrets(self):
        script = "import os,sys; print(os.environ.get('OPENAI_API_KEY','absent')); print(os.environ.get('GITHUB_TOKEN','absent')); print(sys.argv[1])"
        with patch.dict(os.environ, {"OPENAI_API_KEY": "must-not-leak", "GITHUB_TOKEN": "also-secret"}):
            results = run_checks(self.source, [[sys.executable, "-c", script, "literal & echo NOT_A_SHELL"]])
        self.assertEqual(results[0]["exit_code"], 0)
        self.assertEqual(results[0]["stdout"].splitlines(), ["absent", "absent", "literal & echo NOT_A_SHELL"])
        self.assertFalse(results[0]["timed_out"])

    def test_checks_report_failure_missing_command_and_output_limit(self):
        results = run_checks(self.source, [[sys.executable, "-c", "import sys; print('bad',file=sys.stderr); sys.exit(3)"],
                                           ["this-command-does-not-exist-xyz"]])
        self.assertEqual(results[0]["exit_code"], 3)
        self.assertIn("bad", results[0]["stderr"])
        self.assertIsNone(results[1]["exit_code"])
        self.assertTrue(results[1]["stderr"])
        with patch("renew.repository.MAX_CHECK_OUTPUT_BYTES", 20):
            result = run_checks(self.source, [[sys.executable, "-c", "print('x'*1000)"]])[0]
        self.assertEqual(len(result["stdout"]), 20)
        self.assertTrue(result["output_truncated"])

    def test_checks_timeout_terminates_process(self):
        started = time.monotonic()
        result = run_checks(self.source, [[sys.executable, "-c", "import time; print('started',flush=True); time.sleep(60)"]], timeout=1)[0]
        self.assertTrue(result["timed_out"])
        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("started", result["stdout"])
        self.assertLess(time.monotonic() - started, 15)

    def test_checks_reject_shell_strings(self):
        with self.assertRaises(ValueError):
            run_checks(self.source, ["echo bad"])


if __name__ == "__main__":
    unittest.main()
