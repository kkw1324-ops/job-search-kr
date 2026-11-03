"""count_chars.py 가 글자 수 기준과 제한 판정을 지키는지 확인한다."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import count_chars as cc  # noqa: E402


def run_count(text=None, make_dir=True):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        count_dir = root / "work" / "count"
        if make_dir:
            count_dir.mkdir(parents=True)
            if text is not None:
                (count_dir / "a.txt").write_text(text, encoding="utf-8")
        out = io.StringIO()
        with mock.patch.object(cc, "COUNT_DIR", count_dir), mock.patch.object(cc, "ROOT", root), \
                contextlib.redirect_stdout(out):
            code = cc.main()
    return code, out.getvalue()


class MeasureTest(unittest.TestCase):
    TEXT = "한글 ab"

    def test_with_spaces(self):
        self.assertEqual(cc.measure(self.TEXT)["with_spaces"], 5)

    def test_without_spaces(self):
        self.assertEqual(cc.measure(self.TEXT)["without_spaces"], 4)

    def test_bytes_utf8_counts_hangul_as_3(self):
        self.assertEqual(cc.measure(self.TEXT)["bytes_utf8"], 3 * 2 + 1 + 2)

    def test_bytes_cp949_counts_hangul_as_2(self):
        self.assertEqual(cc.measure(self.TEXT)["bytes_cp949"], 2 * 2 + 1 + 2)


class LimitTest(unittest.TestCase):
    def test_over_limit_prints_excess(self):
        _, out = run_count("#limit 10 with_spaces\n" + "가" * 12)
        self.assertIn("초과 2자", out)

    def test_over_only_when_newline_counted_as_two(self):
        _, out = run_count("#limit 10 with_spaces\n" + "가" * 4 + "\n" + "나" * 5)
        self.assertIn("줄바꿈을 2자로 세는 사이트면", out)

    def test_pass_prints_remaining(self):
        _, out = run_count("#limit 10 with_spaces\n" + "가" * 9)
        self.assertIn("통과", out)

    def test_bad_limit_header_reported(self):
        _, out = run_count("#limit abc\n본문")
        self.assertIn("첫 줄 형식이 맞지 않습니다", out)


class MissingInputTest(unittest.TestCase):
    def test_missing_dir_message_shows_limit_header(self):
        code, out = run_count(make_dir=False)
        self.assertEqual(code, 1)
        self.assertIn("#limit", out)

    def test_empty_dir_message_shows_limit_header(self):
        code, out = run_count(text=None)
        self.assertEqual(code, 1)
        self.assertIn("#limit", out)


if __name__ == "__main__":
    unittest.main()
