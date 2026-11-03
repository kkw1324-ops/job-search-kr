"""score.py 가 가중 평균, 감점, 해석 구간, 입력 검사를 지키는지 확인한다."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _common  # noqa: E402
import score  # noqa: E402

NAMES = ["요건 충족", "목표 적합", "보상·조건", "일하는 방식", "회사 상태"]


def run_score(data=None, raw=None):
    """work/score.json 을 만들어 score.main 을 돌리고 (출력, 오류 or None) 을 돌려준다."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "work").mkdir()
        if data is not None:
            (root / "work" / "score.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(score, "WORK", root / "work"), mock.patch.object(_common, "ROOT", root), \
                contextlib.redirect_stdout(out):
            try:
                score.main()
            except _common.UserError as exc:
                return out.getvalue(), str(exc)
    return out.getvalue(), None


def scores(**over):
    base = {name: 3 for name in NAMES}
    base.update(over)
    return base


class BandTest(unittest.TestCase):
    def test_band_priority_at_4_5(self):
        out, err = run_score({"scores": {name: 5 for name in NAMES}, "deductions": [{"reason": "x", "points": 0.5}]})
        self.assertIsNone(err)
        self.assertIn("점수: 4.5 / 5", out)
        self.assertIn("해석: 우선 지원", out)

    def test_band_worth_applying_at_4_0(self):
        out, _ = run_score({"scores": {name: 5 for name in NAMES}, "deductions": [{"reason": "x", "points": 1.0}]})
        self.assertIn("점수: 4.0 / 5", out)
        self.assertIn("해석: 지원할 만함", out)

    def test_band_only_with_reason_at_3_5(self):
        out, _ = run_score({"scores": scores(**{"요건 충족": 4, "목표 적합": 4, "회사 상태": 2}), "deductions": []})
        self.assertIn("점수: 3.5 / 5", out)
        self.assertIn("해석: 특별한 이유가 있을 때만", out)

    def test_band_not_recommended_below_3_5(self):
        out, _ = run_score({"scores": scores(), "deductions": [{"reason": "x", "points": 0.1}]})
        self.assertIn("점수: 2.9 / 5", out)
        self.assertIn("해석: 지원하지 않기를 권함", out)


class InputCheckTest(unittest.TestCase):
    def test_deductions_over_limit_rejected(self):
        _, err = run_score({"scores": scores(), "deductions": [{"reason": "a", "points": 0.6}, {"reason": "b", "points": 0.5}]})
        self.assertIn("한도", err)

    def test_wrong_score_keys_rejected(self):
        bad = scores()
        del bad["회사 상태"]
        _, err = run_score({"scores": bad})
        self.assertIn("다섯 항목", err)

    def test_out_of_range_or_non_integer_rejected(self):
        for value in (0, 6, 3.5):
            _, err = run_score({"scores": scores(**{"요건 충족": value})})
            self.assertIn("1~5 정수", err, value)

    def test_missing_file_message_shows_shape(self):
        _, err = run_score(None)
        self.assertIn('"scores"', err)
        self.assertIn('"deductions"', err)


class WarningTest(unittest.TestCase):
    def test_warning_when_high_total_and_low_work_style(self):
        data = {"scores": scores(**{"요건 충족": 5, "목표 적합": 5, "보상·조건": 5, "회사 상태": 5, "일하는 방식": 2}), "deductions": []}
        out, _ = run_score(data)
        self.assertIn("경고: 총점은 높지만 일하는 방식", out)

    def test_no_warning_when_work_style_is_3(self):
        data = {"scores": scores(**{"요건 충족": 5, "목표 적합": 5, "보상·조건": 5, "회사 상태": 5}), "deductions": []}
        out, _ = run_score(data)
        self.assertNotIn("경고", out)

    def test_no_warning_when_total_below_4_5(self):
        out, _ = run_score({"scores": scores(**{"일하는 방식": 1}), "deductions": []})
        self.assertNotIn("경고", out)


if __name__ == "__main__":
    unittest.main()
