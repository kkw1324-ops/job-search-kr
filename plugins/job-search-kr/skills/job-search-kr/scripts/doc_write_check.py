#!/usr/bin/env python3
"""PreToolUse 훅: 지원 서류 파일을 쓰거나 고칠 때 「쓰기 전 확인」과 검토 단계를 다시 알린다.

플러그인 훅(hooks/hooks.json)에서 부른다. Claude Code(Write·Edit의 file_path)와 Codex(apply_patch의 patch 본문)를 모두 받는다.
표준 입력으로 훅 JSON을 받고, 막지 않고 안내만 돌려준다.
작업 폴더는 `profile/profile.md`가 있는 폴더로 알아본다.
"""
import json
import os
import re
import sys
from pathlib import Path

DOC_NAME = re.compile(r"초안|자소서|자기소개|경력기술서|이력서|소개|프로필|커버레터|커버 레터|양식|cover[-_ ]?letter|resume|(?<![a-z])cv(?![a-z])", re.IGNORECASE)
NOT_DOC = re.compile(r"검토|평가|기업분석|회사분석|공공자료|회사사실|결과|연봉|결정|예상질문|문항|기록")

MESSAGE = (
    "job-search-kr: 지원 서류 파일을 쓰려고 합니다. SKILL.md 「쓰기 전 확인」 다섯 가지"
    "(용어표, 확정 문구·기준본, 결정 메모, 지원 업계의 말, 칸 용도)와 writing-common.md 규칙을 이번 작업에서 마쳤는지 확인하고, "
    "빠진 것이 있으면 지금 채웁니다. 사용자에게 보이기 전에 job-reviewer 검토를 거칩니다."
)


PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Move to): (.+)$", re.MULTILINE)

def is_document(path: str) -> bool:
    path = path.replace("\\", "/")
    roots = [path[:m.start()] for m in re.finditer(r"/(?:applications|work)/", path)]
    if not any(root and (Path(root) / "profile" / "profile.md").is_file() for root in roots):
        return False
    name = path.rsplit("/", 1)[-1]
    return bool(DOC_NAME.search(name)) and not NOT_DOC.search(name)

def target_paths(data: dict) -> list:
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    if tool_input.get("file_path"):
        paths = [tool_input["file_path"]]
    elif isinstance(tool_input.get("command"), str):
        paths = PATCH_FILE.findall(tool_input["command"])
    else:
        return []
    cwd = data.get("cwd") or os.getcwd()
    return [os.path.normpath(os.path.join(cwd, p.strip())) for p in paths]

def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        return
    if not isinstance(data, dict):
        return
    if not any(is_document(p) for p in target_paths(data)):
        return
    print(json.dumps({
        "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": MESSAGE}
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
