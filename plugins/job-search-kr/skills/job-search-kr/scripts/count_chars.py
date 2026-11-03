#!/usr/bin/env python3
"""work/count/*.txt 파일마다 글자 수를 센다.

첫 줄에 `#limit <숫자> <기준>` 을 쓰면 제한 초과 여부도 알려 준다.
기준: with_spaces, without_spaces, bytes_utf8, bytes_cp949
인자를 받지 않는다. 읽는 위치는 항상 work/count/ 이다.
"""
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path.cwd()
COUNT_DIR = ROOT / "work" / "count"
MAX_BYTES = 1_000_000
MODES = ("with_spaces", "without_spaces", "bytes_utf8", "bytes_cp949")
LIMIT_RE = re.compile(r"^#limit\s+(\d+)\s+(\w+)\s*$")


def cp949_len(text):
    total, unencodable = 0, 0
    for ch in text:
        try:
            total += len(ch.encode("cp949"))
        except UnicodeEncodeError:
            total += 2
            unencodable += 1
    return total, unencodable


def measure(text):
    no_space = text.replace(" ", "").replace("　", "")
    no_ws = re.sub(r"\s", "", text)
    newlines = text.count("\n")
    cp949, unencodable = cp949_len(text)
    return {
        "with_spaces": len(text),
        "with_spaces_crlf": len(text) + newlines,
        "without_spaces": len(no_space),
        "without_whitespace": len(no_ws),
        "bytes_utf8": len(text.encode("utf-8")),
        "bytes_cp949": cp949,
        "newlines": newlines,
        "unencodable": unencodable,
    }


def check_limit(m, limit, mode):
    if mode == "with_spaces":
        if m["with_spaces"] > limit:
            return f"초과 {m['with_spaces'] - limit}자"
        if m["with_spaces_crlf"] > limit:
            return f"줄바꿈을 2자로 세는 사이트면 {m['with_spaces_crlf'] - limit}자 초과"
        return f"통과 (남은 {limit - m['with_spaces_crlf']}자, 줄바꿈 2자 기준)"
    if mode == "without_spaces":
        # 사이트마다 줄바꿈 포함 여부가 달라 더 큰 값(스페이스만 뺀 값)으로 판정한다.
        used = m["without_spaces"]
        return f"초과 {used - limit}자" if used > limit else f"통과 (남은 {limit - used}자)"
    used = m[mode]
    return f"초과 {used - limit}바이트" if used > limit else f"통과 (남은 {limit - used}바이트)"


def main():
    if not COUNT_DIR.is_dir():
        print(f"폴더가 없습니다: {COUNT_DIR.relative_to(ROOT)} (.txt 파일의 첫 줄에 `#limit 1000 with_spaces`를 쓰면 제한도 확인합니다. 첫 줄은 선택)")
        return 1
    files = sorted(p for p in COUNT_DIR.iterdir() if p.suffix == ".txt" and not p.name.startswith("."))
    if not files:
        print("work/count/ 에 .txt 파일이 없습니다. (첫 줄 `#limit 1000 with_spaces`는 선택)")
        return 1

    for path in files:
        print(f"== {path.name}")
        if path.is_symlink() or not path.is_file():
            print("  건너뜀: 일반 파일이 아닙니다.")
            continue
        if path.stat().st_size > MAX_BYTES:
            print("  건너뜀: 1MB를 넘습니다.")
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            print("  건너뜀: UTF-8 텍스트가 아닙니다.")
            continue

        raw = raw.replace("\r\n", "\n").replace("\r", "\n")
        lines = raw.split("\n")
        limit = mode = None
        if lines and lines[0].startswith("#limit"):
            match = LIMIT_RE.match(lines[0].strip())
            if not match or match.group(2) not in MODES:
                print(f"  첫 줄 형식이 맞지 않습니다: {lines[0]!r}  (예: #limit 1000 with_spaces)")
            else:
                limit, mode = int(match.group(1)), match.group(2)
            lines = lines[1:]
        text = "\n".join(lines).strip("\n")

        normalized = unicodedata.normalize("NFC", text)
        if normalized != text:
            print("  주의: 자모가 분리된 글자가 있어 합친 뒤 셉니다(맥에서 복사한 글에서 생김).")
        text = normalized

        m = measure(text)
        print(f"  공백 포함: {m['with_spaces']}자 (줄바꿈 {m['newlines']}개를 2자로 세면 {m['with_spaces_crlf']}자)")
        print(f"  공백 제외: {m['without_spaces']}자 (줄바꿈까지 빼면 {m['without_whitespace']}자)")
        print(f"  바이트: UTF-8 {m['bytes_utf8']} / 한글 2바이트 기준 {m['bytes_cp949']}")
        if m["unencodable"]:
            print(f"  주의: 오래된 한글 인코딩으로 표현할 수 없는 글자 {m['unencodable']}개(이모지 등). 사이트에서 깨질 수 있습니다.")
        if limit is not None:
            print(f"  제한 {limit} ({mode}): {check_limit(m, limit, mode)}")
            used = m[mode]
            if limit and used < limit * 0.9:
                print(f"  참고: 제한의 {used * 100 // limit}%만 썼습니다. 보통 90~100%를 채웁니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
