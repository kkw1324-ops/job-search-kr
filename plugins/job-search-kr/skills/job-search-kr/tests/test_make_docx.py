"""make_docx.py 가 자리표시 거부, 중복 파일명, 유효한 docx 출력을 지키는지 확인한다."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _common  # noqa: E402
import make_docx  # noqa: E402


class Env:
    def __init__(self, md=None, meta=None):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "work").mkdir()
        self.set(md, meta)

    def set(self, md=None, meta=None):
        if md is not None:
            (self.root / "work" / "doc.md").write_text(md, encoding="utf-8")
        if meta is not None:
            (self.root / "work" / "doc.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    def run(self):
        out = io.StringIO()
        with mock.patch.object(make_docx, "WORK", self.root / "work"), mock.patch.object(make_docx, "ROOT", self.root), \
                mock.patch.object(_common, "ROOT", self.root), mock.patch.object(_common, "APPLICATIONS", self.root / "applications"), \
                contextlib.redirect_stdout(out):
            try:
                make_docx.main()
            except _common.UserError as exc:
                return str(exc)
        return None

    def outputs(self):
        return sorted(p.name for p in (self.root / "applications").rglob("*.docx")) if (self.root / "applications").exists() else []


META = {"filename": "이력서", "folder": "회사-직무", "lang": "ko"}


class MakeDocxTest(unittest.TestCase):
    def test_placeholder_blocks_output(self):
        for marker in ("[수치 확인 필요]", "[확인 필요]"):
            env = Env(f"# 제목\n처리 시간 {marker}", META)
            err = env.run()
            self.assertIn("확인이 필요한 표시", err)
            self.assertEqual(env.outputs(), [])

    def test_second_run_adds_number_to_name(self):
        env = Env("# 제목\n본문", META)
        self.assertIsNone(env.run())
        self.assertIsNone(env.run())
        self.assertEqual(env.outputs(), ["이력서-2.docx", "이력서.docx"])

    def test_output_is_valid_zip_with_document_xml(self):
        env = Env("# 제목\n본문 문장", META)
        self.assertIsNone(env.run())
        path = env.root / "applications" / "회사-직무" / "이력서.docx"
        with zipfile.ZipFile(path) as z:
            self.assertIsNone(z.testzip())
            self.assertIn("본문 문장", z.read("word/document.xml").decode("utf-8"))

    def test_invalid_lang_rejected(self):
        env = Env("본문", {**META, "lang": "jp"})
        self.assertIn("ko 또는 en", env.run())
        self.assertEqual(env.outputs(), [])

    def test_missing_doc_md_message_names_shape(self):
        env = Env(None, META)
        err = env.run()
        self.assertIn("work/doc.md", err)
        self.assertIn("work/doc.json", err)
        self.assertIn("filename", err)


if __name__ == "__main__":
    unittest.main()
