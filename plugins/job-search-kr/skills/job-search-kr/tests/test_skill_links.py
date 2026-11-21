"""SKILL.md 와 절차 파일의 링크, 훅 안내문이 서로 맞는지 확인한다."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REF = ROOT / "references"
LINK = re.compile(r"\]\(([^)#\s]+\.md)\)")


class LinkTest(unittest.TestCase):
    def test_every_skill_md_link_resolves(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        targets = LINK.findall(text)
        self.assertTrue(targets)
        for t in targets:
            self.assertTrue((ROOT / t).is_file(), t)

    def test_every_reference_link_resolves(self):
        for md in REF.rglob("*.md"):
            for t in LINK.findall(md.read_text(encoding="utf-8")):
                if t.startswith("http"):
                    continue
                target = md.parent / t
                if t.rsplit("/", 2)[-2:-1] == ["agents"] and not target.is_file():
                    # Agent files live in .claude/agents (installed) or next to the skill copy (rewrite folder).
                    name = t.rsplit("/", 1)[-1]
                    target = next((c for c in (ROOT.parent.parent / "agents" / name, ROOT.parent / "agents" / name) if c.is_file()), target)
                self.assertTrue(target.is_file(), f"{md.name} -> {t}")

    def test_evaluate_points_to_dropped_files(self):
        text = (REF / "evaluate.md").read_text(encoding="utf-8")
        for name in ("collect.md", "kr-law.md", "tracker.md"):
            self.assertIn(f"]({name})", text, name)

    def test_tracker_links_analysis_file_and_it_exists(self):
        self.assertIn("](tracker-analysis.md)", (REF / "tracker.md").read_text(encoding="utf-8"))
        analysis = (REF / "tracker-analysis.md").read_text(encoding="utf-8")
        self.assertIn("탈락 패턴 분석", analysis)
        self.assertIn("부족한 역량과 학습 계획", analysis)

    def test_skill_md_holds_process_table_and_setup(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        for heading in ("## 공정표", "## 진입 판정", "## 서류 문구의 필수 과정", "## 생략과 시간 부족", "## 보고 형식", "## 초기 설정"):
            self.assertIn(heading, text, heading)
        for n in range(1, 18):
            self.assertRegex(text, rf"\n\| {n} \| ", f"공정 {n}")
        self.assertNotIn("first-start", text)
        self.assertFalse((REF / "first-start.md").exists())

    def test_writing_log_template_matches_guard(self):
        import sys
        sys.path.insert(0, str(ROOT / "scripts"))
        import doc_write_check as guard
        template = (REF / "writing-log.md").read_text(encoding="utf-8")
        self.assertIn("](references/writing-log.md)", (ROOT / "SKILL.md").read_text(encoding="utf-8"))
        for step in ("① 지원 가능 확인", "④ 칸 설계", "⑩ 제출 준비", "## 사용자 통과"):
            self.assertIn(step, template, step)
        rows = guard.parse_log(template)
        self.assertIsNotNone(rows, "template header must match the guard's HEADER")
        self.assertEqual("".join(sorted(rows)), "".join(sorted(guard.STEPS)))


if __name__ == "__main__":
    unittest.main()
