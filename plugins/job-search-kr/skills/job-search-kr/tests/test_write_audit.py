"""write_audit.py 가 작성기록을 읽어 단계 완료표와 문제를 내는지 확인한다."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import write_audit as audit  # noqa: E402

HEADER = "| 단계 | 산출물 | 찾아본 곳 | 질문과 답 | 생략 승인 | 작성 | 확인 |\n|---|---|---|---|---|---|---|\n"
NAMES = ["① 지원 가능 확인", "② 회사·직무 재료 확인", "③ 경험 재료 확인", "④ 칸 설계", "⑤ 작성",
         "⑥ 작문 심사와 칸 재작성", "⑦ 사실·합격 심사", "⑧ 재심사와 마감", "⑨ 사용자 보고", "⑩ 제출 준비"]


def log_text(rows):
    """rows: {기호: (산출물, 생략 승인, 작성, 확인)}"""
    body = ""
    for n in NAMES:
        out, skip, who, chk = rows.get(n[0], ("", "", "", ""))
        body += f"| {n} | {out} |  |  | {skip} | {who} | {chk} |\n"
    return f"# 작성기록\n\n{HEADER}{body}\n## 사용자 통과\n"


class AuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.work = root / "work"
        self.work.mkdir()
        self.folder = root / "applications" / "회사-직무"
        self.folder.mkdir(parents=True)
        for attr, val in (("ROOT", root), ("TARGET_FILE", self.work / "감사대상.txt"),
                          ("RESULT_FILE", self.work / "감사결과.md")):
            old = getattr(audit, attr)
            setattr(audit, attr, val)
            self.addCleanup(setattr, audit, attr, old)
        (self.work / "감사대상.txt").write_text("\napplications/회사-직무\n", encoding="utf-8")

    def write(self, name, text="x"):
        (self.folder / name).write_text(text, encoding="utf-8")
        return self.folder / name

    def run_audit(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = audit.run()
        return code, out.getvalue()

    def row(self, out, step):
        for line in out.splitlines():
            if line.startswith(f"| {step} "):
                return line
        self.fail(f"{step} row missing")

    def test_status_values(self):
        self.write("a.md")
        self.write("b.md")
        self.write("작성기록.md", log_text({
            "①": ("a.md", "", "메인, 2026-11-01", "검토자, 2026-11-01, a.md"),
            "②": ("b.md", "", "메인, 2026-11-01", ""),
            "③": ("", "2026-11-01 「생략」", "", ""),
        }))
        code, out = self.run_audit()
        self.assertIn("| 완료 |", self.row(out, "①"))
        self.assertIn("| 확인 없음 |", self.row(out, "②"))
        self.assertIn("| 생략 승인 |", self.row(out, "③"))
        self.assertIn("| 없음 |", self.row(out, "⑤"))
        self.assertEqual(code, 1)

    def test_self_check_flagged(self):
        self.write("a.md")
        self.write("작성기록.md", log_text({"①": ("a.md", "", "job-writer, 2026-11-01", "Job-Writer, 2026-11-02, a.md")}))
        code, out = self.run_audit()
        self.assertIn("자기 확인", self.row(out, "①"))
        self.assertEqual(code, 1)

    def test_missing_artifact(self):
        self.write("작성기록.md", log_text({"①": ("없는파일.md", "", "메인, 2026-11-01", "검토자")}))
        _, out = self.run_audit()
        self.assertIn("산출물 파일 없음: 없는파일.md", self.row(out, "①"))

    def test_date_order_violation(self):
        for f in ("a.md", "b.md", "c.md"):
            self.write(f)
        self.write("작성기록.md", log_text({
            "④": ("a.md", "", "메인, 2026-11-05", "검토자"),
            "⑤": ("b.md", "", "job-writer, 2026-11-03", "검토자"),
            "⑥": ("c.md", "", "검토, 2026-11-06", "메인"),
        }))
        _, out = self.run_audit()
        self.assertIn("날짜 순서: ⑤(2026-11-03)가 ④(2026-11-05)", out)
        self.assertNotIn("날짜 순서: ⑥", out)

    def test_missing_date_flagged(self):
        self.write("a.md")
        self.write("작성기록.md", log_text({"①": ("a.md", "", "메인", "검토자")}))
        _, out = self.run_audit()
        self.assertIn("날짜 없음", self.row(out, "①"))

    def test_self_check_only_on_equal_owner(self):
        self.write("a.md")
        self.write("작성기록.md", log_text({"①": ("a.md", "", "메인, 2026-11-01", "메인 대리, 2026-11-02, a.md")}))
        _, out = self.run_audit()
        self.assertNotIn("자기 확인", out)

    def test_placeholder_output_is_empty(self):
        self.write("작성기록.md", log_text({"⑤": ("-", "", "", "")}))
        _, out = self.run_audit()
        self.assertIn("| 없음 |", self.row(out, "⑤"))

    def test_slash_artifact_resolved_from_root(self):
        (self.work / "직무분석").mkdir()
        (self.work / "직무분석" / "X.md").write_text("x", encoding="utf-8")
        self.write("작성기록.md", log_text({
            "②": ("work/직무분석/X.md", "", "메인, 2026-11-01", "검토자"),
            "③": ("work/직무분석/없음.md", "", "메인, 2026-11-01", "검토자"),
        }))
        _, out = self.run_audit()
        self.assertNotIn("산출물 파일 없음: work/직무분석/X.md", out)
        self.assertIn("산출물 파일 없음: work/직무분석/없음.md", out)

    def test_optional_steps_not_problems(self):
        self.write("작성기록.md", log_text({}))
        _, out = self.run_audit()
        self.assertIn("| 없음 |", self.row(out, "⑨"))
        self.assertNotIn("⑨:", out)
        self.assertNotIn("⑩:", out)

    def test_clean_run_exit_zero_and_file_written(self):
        names = {}
        for i, n in enumerate(NAMES):
            fname = f"f{i}.md"
            self.write(fname)
            names[n[0]] = (fname, "", "메인, 2026-11-01", "검토자, 2026-11-02, " + fname)
        self.write("작성기록.md", log_text(names))
        code, out = self.run_audit()
        self.assertEqual(code, 0)
        result = (self.work / "감사결과.md").read_text(encoding="utf-8")
        self.assertEqual(result, out)
        self.assertIn("| 단계 | 상태 | 산출물 | 확인 | 문제 |", result)

    def test_hwpx_output_is_not_cut_to_hwp(self):
        self.write("자소서-초안.hwpx")
        self.write("작성기록.md", log_text({"⑤": ("자소서-초안.hwpx", "", "job-writer, 2026-11-15", "메인")}))
        code, out = self.run_audit()
        self.assertNotIn("산출물 파일 없음", out)

    def test_fully_legitimate_log_is_clean(self):
        for n in ("회사분석.md", "경험.md", "배정표.md", "자소서-초안.md", "검토.md", "제출.md"):
            self.write(n)
        d = "2026-11-15"
        self.write("작성기록.md", log_text({
            "①": ("가능", "", f"메인, {d}", f"job-writer, {d}"),
            "②": ("회사분석.md", "", f"메인, {d}", f"job-writer, {d}"),
            "③": ("경험.md", "", f"메인, {d}", f"job-writer, {d}"),
            "④": ("배정표.md", "", f"job-writer, {d}", f"메인, {d}"),
            "⑤": ("자소서-초안.md", "", f"job-writer, {d}", f"메인, {d}"),
            "⑥": ("검토.md", "", f"job-reviewer, {d}", f"메인, {d}"),
            "⑦": ("검토.md", "", f"job-reviewer, {d}", f"메인, {d}"),
            "⑧": ("검토.md", "", f"job-reviewer, {d}", f"메인, {d}"),
        }))
        code, out = self.run_audit()
        self.assertEqual(code, 0, out)
        self.assertIn("- 없음", out)

    def test_missing_log_is_problem(self):
        code, out = self.run_audit()
        self.assertEqual(code, 1)
        self.assertIn("작성기록.md 가 없습니다", out)

    def test_missing_target_exits_2(self):
        (self.work / "감사대상.txt").unlink()
        code, out = self.run_audit()
        self.assertEqual(code, 2)
        self.assertIn("감사대상.txt", out)

    def test_empty_target_exits_2(self):
        (self.work / "감사대상.txt").write_text("\n  \n", encoding="utf-8")
        self.assertEqual(self.run_audit()[0], 2)

    def test_usage(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(audit.main(["write_audit.py"]), 2)
        self.assertIn("run", out.getvalue())


if __name__ == "__main__":
    unittest.main()
