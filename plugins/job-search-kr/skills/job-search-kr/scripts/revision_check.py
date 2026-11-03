#!/usr/bin/env python3
"""고친 글이 원문의 수치와 뜻을 지키는 표현을 잃지 않았는지 대조한다.

work/revise/<이름>.before.md 와 <이름>.after.md 를 짝지어 읽는다.
수치의 빠짐·추가, 가능·의무·부정·인과·한정 표현의 개수 변화, 바뀐 비율을 알린다.
뜻이 같다는 것을 증명하지 않는다. 걸린 곳은 사람이 다시 읽고 판정한다.
인자를 받지 않는다.
"""
import difflib
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path.cwd()
REVISE_DIR = ROOT / "work" / "revise"
MAX_BYTES = 1_000_000
NUMBER_RE = re.compile(r"[±+\-−]?\d[\d,]*(?:\.\d+)?\s*(?:%|퍼센트|배|건|개|명|시간|분|초|일|주|개월|년|원|만원|억|mm|m|kg|t)?")
MARKERS = {
    "가능": re.compile(r"[ㄹ을] 수 있|할 수 있|수 있"),
    "의무": re.compile(r"야 하|야 했"),
    "부정": re.compile(r"않|못|없었|아니"),
    "인과": re.compile(r"때문|덕분|해서|하여|므로|으로 인해|뒤에|한 뒤"),
    "한정·추산": re.compile(r"약 |일부|추산|추정|대략|정도"),
}
WARN_RATIO, STOP_RATIO = 0.30, 0.50

def numbers(text):
    return Counter(m.group(0).replace(" ", "") for m in NUMBER_RE.finditer(text))

def check(before, after):
    issues = []
    nb, na = numbers(before), numbers(after)
    lost, added = nb - na, na - nb
    if lost:
        issues.append("빠진 수치: " + ", ".join(sorted(lost.elements())))
    if added:
        issues.append("새 수치: " + ", ".join(sorted(added.elements())))
    for name, pattern in MARKERS.items():
        b, a = len(pattern.findall(before)), len(pattern.findall(after))
        if a < b:
            issues.append(f"{name} 표현 줄어듦: {b} → {a}")
        elif a > b and name == "인과":
            issues.append(f"인과 표현 늘어남: {b} → {a}")
    ratio = 1 - difflib.SequenceMatcher(None, before, after).ratio()
    level = "크게 바뀜" if ratio >= STOP_RATIO else ("많이 바뀜" if ratio >= WARN_RATIO else "")
    if level:
        issues.append(f"{level}: 바뀐 비율 {ratio:.0%}")
    return ratio, issues

def read(path):
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"{path.name} 파일이 너무 큽니다.")
    return path.read_text(encoding="utf-8")

def main():
    if not REVISE_DIR.is_dir():
        print("work/revise/ 폴더가 없습니다. <이름>.before.md 와 <이름>.after.md 를 넣어 주세요.")
        return 1
    pairs = sorted(REVISE_DIR.glob("*.before.md"))
    if not pairs:
        print("work/revise/ 에 <이름>.before.md 파일이 없습니다.")
        return 1
    status = 0
    for before_path in pairs:
        name = before_path.name[: -len(".before.md")]
        after_path = REVISE_DIR / f"{name}.after.md"
        if not after_path.exists():
            print(f"{name}: {name}.after.md 가 없습니다.")
            status = 1
            continue
        ratio, issues = check(read(before_path), read(after_path))
        print(f"{name}: 바뀐 비율 {ratio:.0%}, 걸린 곳 {len(issues)}개")
        for issue in issues:
            print(f"  - {issue}")
        if issues:
            status = 2
    return status

if __name__ == "__main__":
    sys.exit(main())
