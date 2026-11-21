#!/usr/bin/env python3
"""자소서 작성 기록을 사후에 감사한다. 사용자에게 보고하기 전에 메인이 돌린다.

사용법: write_audit.py run
읽는 위치는 work/감사대상.txt 의 첫 비어 있지 않은 줄(지원 폴더 경로)이다. 결과는 화면과 work/감사결과.md 에 쓴다.
종료 코드: 0 문제 없음, 1 문제 있음, 2 입력 없음.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from doc_write_check import STEPS, is_filled, parse_log  # noqa: E402

ROOT = Path.cwd()
TARGET_FILE = ROOT / "work" / "감사대상.txt"
RESULT_FILE = ROOT / "work" / "감사결과.md"
FILE_LIKE = re.compile(r"[^\s,;「」`()|]+\.(?:md|docx|txt|hwpx|hwp|pdf|tsv)", re.IGNORECASE)
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
ORDERED = "④⑤⑥⑦⑧"
OPTIONAL_EMPTY = "⑨⑩"  # 보고 전에는 비어 있는 것이 정상


def read_target():
    if not TARGET_FILE.is_file():
        return None
    for line in TARGET_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            p = Path(line).expanduser()
            return p if p.is_absolute() else ROOT / p
    return None


def cell(text):
    return (text or "").replace("|", "/") or "-"


def audit(folder):
    """(표 줄 목록, 문제 목록)을 돌려준다."""
    problems, table, dates = [], [], {}
    log = folder / "작성기록.md"
    if not log.is_file():
        return [], ["작성기록.md 가 없습니다."]
    try:
        rows = parse_log(log.read_text(encoding="utf-8"))
    except UnicodeDecodeError:
        return [], ["작성기록.md 를 UTF-8로 읽지 못했습니다. UTF-8로 저장해야 합니다."]
    if not rows:
        return [], ["작성기록.md 가 양식(references/writing-log.md)을 따르지 않아 읽지 못했습니다."]
    for s in STEPS:
        r = rows.get(s)
        if r is None:
            table.append((s, "없음", "-", "-", "표에 줄이 없음"))
            problems.append(f"{s}: 표에 줄이 없습니다.")
            continue
        out = r["산출물"] if is_filled(r["산출물"]) else ""
        chk = r["확인"] if is_filled(r["확인"]) else ""
        skip = r["생략 승인"] if is_filled(r["생략 승인"]) else ""
        who = r["작성"]
        m = DATE.search(who)
        if m:
            dates[s] = m.group(0)
        notes = []
        if out:
            status = "완료" if chk else "확인 없음"
        elif skip:
            status = "생략 승인"
        else:
            status = "없음"
        if status == "확인 없음":
            notes.append("확인 칸이 비어 있음")
        if status == "없음" and s not in OPTIONAL_EMPTY:
            notes.append("산출물도 생략 승인도 없음")
        if who and chk:
            who_name = who.split(",")[0].strip().casefold()
            if who_name and who_name == r["확인"].split(",")[0].strip().casefold():
                notes.append(f"자기 확인: {who_name}")
        if out and not m:
            notes.append("날짜 없음: 작성 칸에 YYYY-MM-DD 날짜가 없음")
        for name in FILE_LIKE.findall(out):
            target = ROOT / name if "/" in name else folder / name
            if not target.exists():
                notes.append(f"산출물 파일 없음: {name}")
        table.append((s, status, out, chk, "; ".join(notes)))
        problems.extend(f"{s}: {n}" for n in notes)
    problems.extend(order_problems(dates))
    return table, problems


def order_problems(dates):
    """작성 칸 날짜가 ④≤⑤≤⑥≤⑦≤⑧ 순서인지 본다(날짜가 있는 단계끼리만)."""
    found, prev = [], None
    for s in ORDERED:
        if s not in dates:
            continue
        if prev and dates[s] < dates[prev]:
            found.append(f"날짜 순서: {s}({dates[s]})가 {prev}({dates[prev]})보다 앞섭니다.")
        prev = s
    return found


def render(folder, table, problems):
    lines = [f"# 단계 완료표: {folder.name}", "",
             "| 단계 | 상태 | 산출물 | 확인 | 문제 |", "|---|---|---|---|---|"]
    for row in table:
        lines.append("| " + " | ".join(cell(c) for c in row) + " |")
    lines += ["", "## 문제"]
    lines += [f"- {p}" for p in problems] if problems else ["- 없음"]
    return "\n".join(lines) + "\n"


def run():
    folder = read_target()
    if folder is None:
        print("work/감사대상.txt 가 없거나 비어 있습니다. 첫 줄에 지원 폴더 경로(예: applications/회사-직무)를 쓰고 다시 돌립니다.")
        return 2
    if not folder.is_dir():
        print(f"지원 폴더가 없습니다: {folder}")
        return 2
    table, problems = audit(folder)
    text = render(folder, table, problems)
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    RESULT_FILE.write_text(text, encoding="utf-8")
    print(text, end="")
    return 1 if problems else 0


def main(argv):
    if len(argv) != 2 or argv[1] != "run":
        print("사용법: write_audit.py run")
        return 2
    return run()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
