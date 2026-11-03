"""pdf_unlock.py 가 인자 없이 실행하면 암호를 묻지 않고 끝나는지, 암호를 남기지 않는지 확인한다."""
import subprocess
import sys
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "pdf_unlock.py"


class PdfUnlockTest(unittest.TestCase):
    def test_no_args_exits_nonzero_without_prompt(self):
        r = subprocess.run([sys.executable, "-B", str(SCRIPT)], input="", capture_output=True, text=True, timeout=30)
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("PDF 암호 입력", r.stdout + r.stderr)

    def test_script_reads_password_with_getpass_and_never_prints_it(self):
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertIn("getpass.getpass", text)
        self.assertNotIn("print(pw", text)


if __name__ == "__main__":
    unittest.main()
