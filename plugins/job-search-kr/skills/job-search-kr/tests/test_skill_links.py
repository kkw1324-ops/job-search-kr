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
                self.assertTrue((md.parent / t).is_file(), f"{md.name} -> {t}")

    def test_evaluate_points_to_dropped_files(self):
        text = (REF / "evaluate.md").read_text(encoding="utf-8")
        for name in ("collect.md", "kr-law.md", "tracker.md"):
            self.assertIn(f"]({name})", text, name)

    def test_tracker_links_analysis_file_and_it_exists(self):
        self.assertIn("](tracker-analysis.md)", (REF / "tracker.md").read_text(encoding="utf-8"))
        analysis = (REF / "tracker-analysis.md").read_text(encoding="utf-8")
        self.assertIn("탈락 패턴 분석", analysis)
        self.assertIn("부족한 역량과 학습 계획", analysis)

    def test_skill_md_routes_first_start_file(self):
        self.assertIn("references/first-start.md", (ROOT / "SKILL.md").read_text(encoding="utf-8"))
        self.assertTrue((REF / "first-start.md").is_file())


if __name__ == "__main__":
    unittest.main()
