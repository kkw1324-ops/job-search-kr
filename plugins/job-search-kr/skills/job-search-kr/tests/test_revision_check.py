"""revision_check 가 수치·뜻 표현 변화와 바뀐 비율을 잡는지 확인한다."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import revision_check as rc  # noqa: E402


class CheckTest(unittest.TestCase):
    def test_unchanged_text_has_no_issue(self):
        text = "회의록 정리 시간을 약 30분에서 10분으로 줄였습니다."
        ratio, issues = rc.check(text, text)
        self.assertEqual(issues, [])
        self.assertEqual(ratio, 0)

    def test_lost_and_added_numbers(self):
        _, issues = rc.check("고객 문의가 약 50% 줄었습니다.", "고객 문의가 60% 줄었습니다.")
        joined = "\n".join(issues)
        self.assertIn("빠진 수치: 50%", joined)
        self.assertIn("새 수치: 60%", joined)

    def test_hedge_removed(self):
        _, issues = rc.check("처리 시간이 약 62% 줄었습니다.", "처리 시간이 62% 줄었습니다.")
        self.assertTrue(any("한정·추산" in i for i in issues))

    def test_modal_removed(self):
        _, issues = rc.check("현장에서 바로 고칠 수 있게 했습니다.", "현장에서 바로 고쳤습니다.")
        self.assertTrue(any("가능" in i for i in issues))

    def test_negation_removed(self):
        _, issues = rc.check("모든 배송 지연은 막지 못했습니다.", "모든 배송 지연을 막았습니다.")
        self.assertTrue(any("부정" in i for i in issues))

    def test_new_causal_link(self):
        _, issues = rc.check("기준을 정했습니다. 문의가 줄었습니다.", "기준을 정했기 때문에 문의가 줄었습니다.")
        self.assertTrue(any("인과 표현 늘어남" in i for i in issues))

    def test_large_rewrite_flagged(self):
        _, issues = rc.check("계약 조건을 검토했습니다.", "고객 요구를 분석해 새 시스템을 설계하고 구축했습니다.")
        self.assertTrue(any("크게 바뀜" in i for i in issues))


if __name__ == "__main__":
    unittest.main()
