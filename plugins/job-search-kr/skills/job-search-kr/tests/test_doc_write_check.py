"""doc_write_check 훅이 지원 서류에만 안내를 붙이는지 확인한다."""
import contextlib
import io
import json
import os
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import doc_write_check as hook  # noqa: E402

TMP = tempfile.TemporaryDirectory()
BASE = str(Path(TMP.name).resolve() / "구직")
(Path(BASE) / "profile").mkdir(parents=True)
(Path(BASE) / "profile" / "profile.md").write_text("# 프로필\n", encoding="utf-8")


def run_payload(payload) -> str:
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")), encoding="utf-8")
    out = io.StringIO()
    with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out):
        hook.main()
    return out.getvalue()


def codex_input(patch_path: str, cwd: str = BASE) -> dict:
    patch = f"*** Begin Patch\n*** Add File: {patch_path}\n+초안\n*** End Patch\n"
    return {"tool_name": "apply_patch", "cwd": cwd, "tool_input": {"command": patch}}


def run_hook(path: str, agent_type=None) -> str:
    payload = {"tool_name": "Write", "tool_input": {"file_path": path}}
    if agent_type:
        payload["agent_type"] = agent_type
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")), encoding="utf-8")
    out = io.StringIO()
    with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out):
        hook.main()
    return out.getvalue()


class IsDocumentTest(unittest.TestCase):
    def test_drafts_in_applications(self):
        for name in ["자소서.md", "자소서-v3.md", "이력서-경력기술서-초안.md", "경력기술서.md", "양식-칸별.md"]:
            self.assertTrue(hook.is_document(f"{BASE}/applications/회사-직무/{name}"), name)

    def test_non_documents_in_applications(self):
        for name in ["평가.md", "기업분석.md", "공공자료.md", "검토.md", "요약-검토.md",
                     "결과.md", "결정-메모.md", "미끼-예상질문.md", "연봉자료.md", "회사사실.md"]:
            self.assertFalse(hook.is_document(f"{BASE}/applications/회사-직무/{name}"), name)

    def test_work_folder(self):
        self.assertTrue(hook.is_document(f"{BASE}/work/사이트프로필-초안.md"))
        self.assertFalse(hook.is_document(f"{BASE}/work/자소서-작성기록.md"))
        self.assertFalse(hook.is_document(f"{BASE}/work/score.json"))
        self.assertFalse(hook.is_document(f"{BASE}/work/회사/가나다/회사분석.md"))

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
        self.assertTrue(hook.is_essay(f"{nested}/applications/회사-직무/자소서.md"))


class EssayDetectionTest(unittest.TestCase):
    def test_not_doc_words_do_not_bypass(self):
        for name in ["자소서-결과반영.md", "자소서-최종결정본.md", "자소서-초안-1판-검토전.md"]:
            self.assertTrue(hook.is_essay(f"{BASE}/applications/회사-직무/{name}"), name)
            self.assertTrue(hook.is_document(f"{BASE}/applications/회사-직무/{name}"), name)
            self.assertIsNotNone(hook.essay_location(f"{BASE}/applications/회사-직무/{name}"), name)

    def test_exempt_kinds_are_not_essays(self):
        for name in ["자소서-검토.md", "자소서-검토-2차.md", "자소서-작성기록.md", "자소서-배정표.md"]:
            self.assertFalse(hook.is_essay(f"{BASE}/applications/회사-직무/{name}"), name)
            self.assertIsNone(hook.essay_location(f"{BASE}/applications/회사-직무/{name}"), name)


class MainTest(unittest.TestCase):
    def test_message_for_document(self):
        data = json.loads(run_hook(f"{BASE}/applications/회사-직무/이력서.md"))
        self.assertIn("쓰려고 합니다", data["hookSpecificOutput"]["additionalContext"])

    def test_silent_for_non_document(self):
        self.assertEqual(run_hook(f"{BASE}/applications/회사-직무/평가.md"), "")


    def test_site_profile_drafts(self):
        self.assertTrue(hook.is_document(f"{BASE}/work/remember/소개-최종.md"))
        self.assertTrue(hook.is_document(f"{BASE}/work/사이트프로필-수정안-전후.md"))

    def test_notes_still_excluded_after_adding_names(self):
        self.assertFalse(hook.is_document(f"{BASE}/work/사이트프로필-검토-기록.md"))

    def test_message_points_to_skill_steps(self):
        skill = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("## 서류 문구의 필수 과정", skill)
        self.assertIn("서류 문구의 필수 과정", hook.MESSAGE)
        self.assertIn("job-reviewer", hook.MESSAGE)


    def _assert_not_essay(self, name):
        path = f"{BASE}/applications/회사-직무/{name}"
        self.assertFalse(hook.is_essay(path), name)
        self.assertIsNone(hook.essay_location(path), name)

    def test_review_result_note_is_not_essay(self):
        self._assert_not_essay("자소서-검토결과.md")

    def test_expected_questions_file_is_not_essay(self):
        self._assert_not_essay("자소서-면접예상질문.md")

    def test_char_count_file_is_not_essay(self):
        self._assert_not_essay("자소서-글자수확인.md")

    def test_memo_underscore_is_not_essay(self):
        self._assert_not_essay("자소서_메모.md")

    def test_bak_copy_is_not_essay(self):
        self._assert_not_essay("자소서.md.bak")

    def test_essay_forms_still_essays(self):
        for name in ["자소서.md", "자소서.docx", "자소서.txt", "자소서.hwp", "자소서.hwpx", "자소서-초안.md", "자기소개.md"]:
            self.assertTrue(hook.is_essay(f"{BASE}/applications/회사-직무/{name}"), name)

    def test_draft_names_are_essays(self):
        for name in ["자소서-문항1-초안.md", "지원서-초안.md", "문항답-초안.md"]:
            self.assertTrue(hook.is_essay(f"{BASE}/applications/회사-직무/{name}"), name)
            self.assertIsNotNone(hook.essay_location(f"{BASE}/applications/회사-직무/{name}"), name)

    def test_question_list_without_draft_stays_exempt(self):
        self._assert_not_essay("문항.md")

    def test_essay_name_with_question_word_is_guarded(self):
        path = f"{BASE}/applications/회사-직무/자소서-문항1.md"
        self.assertTrue(hook.is_essay(path))
        self.assertTrue(hook.is_document(path))

    def test_self_intro_name_with_question_word_is_guarded(self):
        self.assertTrue(hook.is_essay(f"{BASE}/applications/회사-직무/자기소개서-문항2.md"))

    def test_bare_question_file_with_number_stays_exempt(self):
        self._assert_not_essay("문항1.md")

    def test_backup_style_draft_stays_exempt(self):
        self._assert_not_essay("자소서-초안-백업.md")

    def test_resume_draft_is_not_essay(self):
        for name in ["이력서-초안.md", "경력기술서-초안.md"]:
            self._assert_not_essay(name)


HEADER = "| 단계 | 산출물 | 찾아본 곳 | 질문과 답 | 생략 승인 | 작성 | 확인 |\n|---|---|---|---|---|---|---|\n"
NAMES = ["① 지원 가능 확인", "② 회사·직무 재료 확인", "③ 경험 재료 확인", "④ 칸 설계", "⑤ 작성",
         "⑥ 작문 심사와 칸 재작성", "⑦ 사실·합격 심사", "⑧ 재심사와 마감", "⑨ 사용자 보고", "⑩ 제출 준비"]


def make_log(filled=None, skipped=None, passes=""):
    filled, skipped = filled or {}, skipped or {}
    rows = ""
    for i, n in enumerate(NAMES):
        k = n[0]
        rows += f"| {n} | {filled.get(k, '')} |  |  | {skipped.get(k, '')} |  |  |\n"
    return f"# 작성기록: x\n\n{HEADER}{rows}\n## 사용자 통과\n\n{passes}\n"


FULL = {"①": "가능", "②": "회사분석.md", "③": "경험.md", "④": "배정표.md"}


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.join(os.path.realpath(self.tmp.name), "구직")
        os.makedirs(os.path.join(self.root, "profile"))
        with open(os.path.join(self.root, "profile", "profile.md"), "w", encoding="utf-8") as f:
            f.write("# 프로필\n")
        self.folder = os.path.join(self.root, "applications", "회사-직무")
        os.makedirs(self.folder)
        self.essay = os.path.join(self.folder, "자소서-초안.md")

    def log(self, text):
        with open(os.path.join(self.folder, "작성기록.md"), "w", encoding="utf-8") as f:
            f.write(text)

    def touch(self, name):
        with open(os.path.join(self.folder, name), "w", encoding="utf-8") as f:
            f.write("x")

    def result(self, path=None, agent=None):
        out = run_hook(path or self.essay, agent)
        return json.loads(out)["hookSpecificOutput"] if out else None

    def assertDenied(self, res, *words):
        self.assertEqual(res["permissionDecision"], "deny")
        for w in words:
            self.assertIn(w, res["permissionDecisionReason"])

    def test_nfd_path_is_normalized(self):
        nfd = unicodedata.normalize("NFD", self.essay)
        self.assertDenied(self.result(nfd), "작성기록.md")

    def test_legacy_folder_guidance_only(self):
        work = os.path.join(self.root, "work")
        os.makedirs(work)
        with open(os.path.join(work, "legacy-folders.txt"), "w", encoding="utf-8") as f:
            f.write("# 주석\n회사-직무\n")
        # 옛 폴더: job-writer 의 초안 저장은 작성기록 없이도 안내만 한다.
        res = self.result(agent="job-writer")
        self.assertNotIn("permissionDecision", res)
        self.assertIn("additionalContext", res)
        # 옛 폴더에서도 메인 대화의 초안 저장은 막는다.
        self.assertDenied(self.result(), "job-writer")
        # 초안이 아닌 자소서 파일은 옛 폴더에서 안내만 한다.
        res = self.result(os.path.join(self.folder, "자소서.md"))
        self.assertNotIn("permissionDecision", res)
        self.assertIn("additionalContext", res)

    def test_missing_log_denies(self):
        self.assertDenied(self.result(), "writing-log.md", "①~④")

    def test_incomplete_steps_deny_and_list(self):
        self.log(make_log({"①": "가능", "③": "경험.md"}))
        res = self.result(agent="job-writer")
        self.assertDenied(res, "②", "④")

    def test_missing_assignment_table_denies(self):
        self.log(make_log(FULL))
        self.assertDenied(self.result(agent="job-writer"), "배정표.md")

    def test_skip_approval_on_step4_waives_assignment(self):
        filled = {k: v for k, v in FULL.items() if k != "④"}
        self.log(make_log(filled, {"④": "2026-01-01 「생략해도 돼」"}))
        res = self.result(agent="job-writer")
        self.assertNotIn("permissionDecision", res)
        self.assertIn("⑥", res["additionalContext"])

    def test_all_steps_done_allows_with_reminder(self):
        self.log(make_log(FULL))
        self.touch("배정표.md")
        res = self.result(agent="job-writer")
        self.assertNotIn("permissionDecision", res)
        self.assertIn("⑧", res["additionalContext"])

    def test_user_pass_allows(self):
        self.log(make_log(passes="- 2026-01-01 자소서-초안.md: 「그냥 저장해」"))
        res = self.result()
        self.assertNotIn("permissionDecision", res)
        self.assertIn("사용자", res["additionalContext"])

    def test_unparseable_log_denies(self):
        self.log("# 작성기록\n\n그냥 메모\n")
        self.assertDenied(self.result(), "양식")

    def test_missing_step_row_denies_as_unparseable(self):
        self.log(make_log(FULL).replace("| ③ 경험 재료 확인 | 경험.md |  |  |  |  |  |\n", ""))
        self.touch("배정표.md")
        self.assertDenied(self.result(), "양식")

    def test_draft_by_other_agent_denied(self):
        self.log(make_log(FULL))
        self.touch("배정표.md")
        self.assertDenied(self.result(agent="job-reviewer"), "job-writer")

    def test_main_session_draft_denied(self):
        self.log(make_log(FULL))
        self.touch("배정표.md")
        self.assertDenied(self.result(), "job-writer", "Bash")

    def test_user_pass_allows_main_draft(self):
        self.log(make_log(passes="- 2026-01-01 자소서-초안.md: 「그냥 저장해」"))
        self.assertNotIn("permissionDecision", self.result())

    def test_placeholder_cells_do_not_count(self):
        self.log(make_log({"①": "-", "②": "—", "③": ".", "④": "배정표.md"}))
        self.touch("배정표.md")
        self.assertDenied(self.result(agent="job-writer"), "①", "②", "③")

    def test_cp949_log_denied(self):
        with open(os.path.join(self.folder, "작성기록.md"), "wb") as f:
            f.write(make_log(FULL).encode("cp949"))
        self.assertDenied(self.result(agent="job-writer"), "UTF-8")

    def test_not_doc_word_essay_guarded_end_to_end(self):
        self.assertDenied(self.result(os.path.join(self.folder, "자소서-결과반영.md")), "작성기록.md")

    def test_review_and_question_files_silent(self):
        self.assertIsNone(self.result(os.path.join(self.folder, "자소서-검토.md")))
        self.assertIsNone(self.result(os.path.join(self.folder, "문항1.md")))

    def test_draft_by_job_writer_allowed(self):
        self.log(make_log(FULL))
        self.touch("배정표.md")
        self.assertNotIn("permissionDecision", self.result(agent="job-writer"))

    def test_main_session_final_allowed(self):
        self.log(make_log(FULL))
        self.touch("배정표.md")
        res = self.result(os.path.join(self.folder, "자소서-최종본.md"))
        self.assertNotIn("permissionDecision", res)

    def test_nested_essay_uses_folder_log(self):
        self.assertDenied(self.result(os.path.join(self.folder, "하위", "자기소개.md")), "작성기록.md")

    def test_non_essay_document_guidance_mentions_skill_steps(self):
        res = self.result(os.path.join(self.folder, "이력서.md"))
        self.assertIn("서류 문구의 필수 과정", res["additionalContext"])

    def test_log_and_assignment_files_not_documents(self):
        self.assertFalse(hook.is_document(os.path.join(self.folder, "배정표.md")))
        self.assertFalse(hook.is_document(os.path.join(self.folder, "작성기록.md")))

    def test_edit_tool_input_is_guarded(self):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": self.essay, "old_string": "a", "new_string": "b"}}
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps(payload, ensure_ascii=False).encode("utf-8")), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out):
            hook.main()
        self.assertDenied(json.loads(out.getvalue())["hookSpecificOutput"], "작성기록.md")

    def test_row_without_trailing_pipe_and_empty_last_cell_parses(self):
        rows = hook.parse_log(HEADER + "| ① 지원 가능 확인 | 가능 | | | | |\n")
        self.assertEqual(rows["①"]["확인"], "")
        self.assertEqual(rows["①"]["산출물"], "가능")

    def test_log_with_short_rows_allows_when_filled(self):
        text = HEADER + "".join(
            f"| {n} | {FULL.get(n[0], '')} | | | | |\n" for n in NAMES)
        self.log(text)
        self.touch("배정표.md")
        self.assertNotIn("permissionDecision", self.result(agent="job-writer"))

    def test_missing_legacy_file_means_no_legacy_folders(self):
        # setUp's workspace has no work/legacy-folders.txt: no crash, folder is guarded normally.
        self.assertEqual(hook.legacy_folders(self.root), set())
        self.assertDenied(self.result(), "작성기록.md")

    def test_codex_patch_essay_without_log_denied(self):
        res = json.loads(run_payload(codex_input("applications/회사-직무/자소서-초안.md", cwd=self.root)))
        self.assertDenied(res["hookSpecificOutput"], "작성기록.md")

    def test_codex_patch_draft_allowed_when_steps_done(self):
        # Codex sends no agent name, so the job-writer ownership check is skipped there.
        self.log(make_log(FULL))
        self.touch("배정표.md")
        res = json.loads(run_payload(codex_input("applications/회사-직무/자소서-초안.md", cwd=self.root)))
        self.assertNotIn("permissionDecision", res["hookSpecificOutput"])

    def test_codex_patch_non_essay_document_guidance(self):
        res = json.loads(run_payload(codex_input("applications/회사-직무/이력서.md", cwd=self.root)))
        self.assertIn("additionalContext", res["hookSpecificOutput"])
        self.assertEqual(run_payload(codex_input("applications/회사-직무/평가.md", cwd=self.root)), "")

    def test_bad_input_is_silent(self):
        self.assertEqual(run_payload({"tool_input": "x"}), "")
        self.assertEqual(run_payload([1]), "")

    def test_malformed_stdin_silent(self):
        for raw in (b"not json", b"\xff\xfe\x00"):
            stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
            out = io.StringIO()
            with mock.patch.object(sys, "stdin", stdin), contextlib.redirect_stdout(out):
                hook.main()
            self.assertEqual(out.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
