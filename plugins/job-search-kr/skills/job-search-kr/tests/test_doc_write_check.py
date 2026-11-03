"""doc_write_check 훅이 지원 서류에만 안내를 붙이는지 확인한다."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import doc_write_check as hook  # noqa: E402

TMP = tempfile.TemporaryDirectory()
BASE = str(Path(TMP.name).resolve() / "구직")
(Path(BASE) / "profile").mkdir(parents=True)
(Path(BASE) / "profile" / "profile.md").write_text("# 프로필\n", encoding="utf-8")

def run_hook(data: dict) -> str:
    stdin = io.StringIO(json.dumps(data))
    out = io.StringIO()
    with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out):
        hook.main()
    return out.getvalue()

def claude_input(path: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": path}}

def codex_input(patch_path: str, cwd: str = BASE) -> dict:
    patch = f"*** Begin Patch\n*** Add File: {patch_path}\n+초안\n*** End Patch\n"
    return {"tool_name": "apply_patch", "cwd": cwd, "tool_input": {"command": patch}}

class IsDocumentTest(unittest.TestCase):
    def test_drafts_in_applications(self):
        for name in ["자소서.md", "자소서-v3.md", "이력서-경력기술서-초안.md", "경력기술서.md", "양식-칸별.md"]:
            self.assertTrue(hook.is_document(f"{BASE}/applications/회사-직무/{name}"), name)

    def test_non_documents_in_applications(self):
        for name in ["평가.md", "기업분석.md", "회사분석.md", "공공자료.md", "검토.md", "요약-검토.md",
                     "결과.md", "결정-메모.md", "미끼-예상질문.md", "연봉자료.md", "회사사실.md"]:
            self.assertFalse(hook.is_document(f"{BASE}/applications/회사-직무/{name}"), name)

    def test_work_folder(self):
        self.assertTrue(hook.is_document(f"{BASE}/work/사이트프로필-초안.md"))
        self.assertFalse(hook.is_document(f"{BASE}/work/자소서-작성기록.md"))
        self.assertFalse(hook.is_document(f"{BASE}/work/score.json"))

    def test_english_and_unrelated_names(self):
        for name in ["Resume.md", "영문CV.md", "CV-en.md", "cover-letter.md", "커버레터.md"]:
            self.assertTrue(hook.is_document(f"{BASE}/applications/회사-직무/{name}"), name)
        for name in ["커버리지.md", "discover-notes.md"]:
            self.assertFalse(hook.is_document(f"{BASE}/work/{name}"), name)

    def test_folder_without_profile(self):
        self.assertFalse(hook.is_document(f"{TMP.name}/other/applications/x/자소서.md"))
        self.assertFalse(hook.is_document(f"{BASE}/profile/경력기술서-기준본.md"))

    def test_workspace_inside_a_work_folder(self):
        nested = Path(TMP.name).resolve() / "work" / "구직2"
        (nested / "profile").mkdir(parents=True)
        (nested / "profile" / "profile.md").write_text("# 프로필\n", encoding="utf-8")
        self.assertTrue(hook.is_document(f"{nested}/work/자소서-초안.md"))

class MainTest(unittest.TestCase):
    def test_message_for_document(self):
        data = json.loads(run_hook(claude_input(f"{BASE}/applications/회사-직무/자소서.md")))
        self.assertIn("쓰려고 합니다", data["hookSpecificOutput"]["additionalContext"])

    def test_silent_for_non_document(self):
        self.assertEqual(run_hook(claude_input(f"{BASE}/applications/회사-직무/평가.md")), "")

    def test_codex_patch_relative_path(self):
        data = json.loads(run_hook(codex_input("applications/회사-직무/자소서.md")))
        self.assertEqual(data["hookSpecificOutput"]["hookEventName"], "PreToolUse")

    def test_codex_patch_non_document(self):
        self.assertEqual(run_hook(codex_input("applications/회사-직무/평가.md")), "")

    def test_bad_input_is_silent(self):
        self.assertEqual(run_hook({"tool_input": "x"}), "")

    def test_non_object_json_is_silent(self):
        self.assertEqual(run_hook([1]), "")

    def test_site_profile_drafts(self):
        self.assertTrue(hook.is_document(f"{BASE}/work/remember/소개-최종.md"))
        self.assertTrue(hook.is_document(f"{BASE}/work/사이트프로필-수정안-2025-01-15-전후.md"))

    def test_notes_still_excluded_after_adding_names(self):
        self.assertFalse(hook.is_document(f"{BASE}/work/사이트프로필-검토-기록.md"))

    def test_message_lists_skill_items(self):
        skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
        block = skill.split("### 쓰기 전 확인", 1)[1].split("\n## ", 1)[0]
        for item in ["용어표", "확정 문구", "결정 메모", "지원 업계의 말", "칸 용도"]:
            self.assertIn(item, block, item)
            self.assertIn(item, hook.MESSAGE, item)


if __name__ == "__main__":
    unittest.main()
