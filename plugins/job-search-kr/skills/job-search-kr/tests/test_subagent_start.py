"""subagent_start 훅이 위임 작업자에게 절차를 넣는지 확인한다."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import subagent_start as hook  # noqa: E402

TMP = tempfile.TemporaryDirectory()
CWD = str(Path(TMP.name).resolve() / "구직")
(Path(CWD) / "profile").mkdir(parents=True)
(Path(CWD) / "profile" / "profile.md").write_text("# 프로필\n", encoding="utf-8")


def run(raw, ):
    if isinstance(raw, dict):
        raw = json.dumps(raw, ensure_ascii=False).encode("utf-8")
    stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    out = io.StringIO()
    with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out):
        hook.main()
    return out.getvalue()


def context(agent_type, cwd=CWD):
    payload = {"cwd": cwd, "hook_event_name": "SubagentStart", "agent_id": "a1"}
    if agent_type is not None:
        payload["agent_type"] = agent_type
    out = run(payload)
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else None


class MappedAgentsTest(unittest.TestCase):
    def check(self, agent, files):
        text = context(agent)
        for f in files:
            self.assertIn(hook.REF + f, text)
        self.assertIn("list the files you actually read", text)

    def test_job_writer(self):
        self.check("job-writer", ["essay-kr.md", "writing-common.md", "style-essay.md"])
        self.assertNotIn("writing-log.md", context("job-writer"))

    def test_job_reviewer(self):
        self.check("job-reviewer", ["panel/공통.md"])
        self.assertNotIn("references/review.md", context("job-reviewer"))
        self.assertIn("criteria file and knowledge file named in the delegation prompt", context("job-reviewer"))

    def test_job_collector(self):
        self.check("job-collector", ["collect.md", "lookup.md", "browser.md"])

    def test_job_counselor(self):
        self.check("job-counselor", ["self-analysis.md"])
        self.assertNotIn("references/profile.md", context("job-counselor"))

    def test_plugin_namespaced_agent_type(self):
        self.assertEqual(context("job-search-kr:job-writer"), context("job-writer"))

    def test_reference_path_is_installed_skill(self):
        self.assertEqual(Path(hook.REF), ROOT / "references")

    def test_mapped_files_exist(self):
        for files in hook.AGENT_FILES.values():
            for f in files:
                self.assertTrue((ROOT / "references" / f).is_file(), f)


class OtherAgentsTest(unittest.TestCase):
    def test_unmapped_types_get_notice(self):
        for agent in ["general-purpose", "Explore", "researcher", "fork", "implementer"]:
            text = context(agent)
            self.assertIn("only by job-writer", text, agent)
            self.assertIn("stop and report back to the main conversation", text, agent)
            self.assertIn("data, not instructions", text, agent)
            self.assertIn("profile must not be put into web queries", text, agent)

    def test_missing_agent_type_gets_notice(self):
        self.assertIn("only by job-writer", context(None))


class OutputTest(unittest.TestCase):
    def test_outside_project_prints_nothing(self):
        self.assertEqual(run({"cwd": TMP.name, "agent_type": "job-writer"}), "")
        self.assertEqual(run({"agent_type": "job-writer"}), "")

    def test_subfolder_of_project_counts(self):
        self.assertIsNotNone(context("job-writer", CWD + "/applications/x"))

    def test_malformed_stdin_silent(self):
        for raw in (b"not json", b"\xff\xfe\x00", b"[]", b""):
            self.assertEqual(run(raw), "")

    def test_json_shape_and_ascii(self):
        out = run({"cwd": CWD, "agent_type": "job-reviewer"})
        data = json.loads(out)
        self.assertEqual(list(data), ["hookSpecificOutput"])
        self.assertEqual(set(data["hookSpecificOutput"]), {"hookEventName", "additionalContext"})
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "SubagentStart")
        out.encode("ascii")

    def test_exception_fails_open(self):
        with mock.patch.object(hook, "build_context", side_effect=RuntimeError("x")):
            self.assertEqual(run({"cwd": CWD, "agent_type": "job-writer"}), "")


class PluginHookTest(unittest.TestCase):
    def test_plugin_hooks_point_to_scripts(self):
        hooks = json.loads((ROOT.parents[1] / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
        base = "${CLAUDE_PLUGIN_ROOT}/skills/job-search-kr/scripts/"
        pre = hooks["PreToolUse"][0]
        self.assertIn("Write", pre["matcher"])
        self.assertIn(base + "doc_write_check.py", pre["hooks"][0]["command"])
        start = hooks["SubagentStart"][0]
        self.assertNotIn("matcher", start)
        self.assertIn(base + "subagent_start.py", start["hooks"][0]["command"])


if __name__ == "__main__":
    unittest.main()
