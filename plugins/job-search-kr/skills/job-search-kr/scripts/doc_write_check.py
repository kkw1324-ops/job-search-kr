#!/usr/bin/env python3
"""PreToolUse 훅: 지원 서류 저장을 지키는 저장 가드.

플러그인 훅(hooks/hooks.json)에서 부른다. Claude Code(Write·Edit의 file_path)와 Codex(apply_patch의 patch 본문)를 모두 받는다.
작업 폴더는 `profile/profile.md`가 있는 폴더로 알아본다.
표준 입력으로 훅 JSON을 받고, 표준 출력에 JSON 한 개를 내거나 아무것도 내지 않는다. 종료 코드는 항상 0.

- 자소서 파일(applications/<폴더>/ 아래, 이름에 자소서·자기소개, 또는 지원서·문항과 초안, 확장자 md·docx·txt·hwp·hwpx. 메모·글자수·예상질문·검토결과·백업 파일은 아니다): 작성기록.md 의 ①~④ 가 갖춰지지 않으면 막는다.
  양식은 references/writing-log.md. 옛 폴더(작업 폴더의 work/legacy-folders.txt에 적은 지원 폴더 이름)는 작성기록을 요구하지 않고 안내만 하되, 초안은 job-writer가 아니면 막는다.
  초안을 job-writer만 저장하는 확인은 에이전트 이름이 훅 입력에 오는 Claude Code에서만 한다. Codex 입력은 작성기록 확인까지만 한다.
- 그 밖의 지원 서류: 안내만 한다.
- 입력 JSON을 읽지 못하면 아무것도 내지 않고, 뜻밖의 오류는 막지 않고 안내만 한다.
"""
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

DOC_NAME = re.compile(r"초안|자소서|자기소개|경력기술서|이력서|소개|프로필|커버레터|커버 레터|양식|cover[-_ ]?letter|resume|(?<![a-z])cv(?![a-z])", re.IGNORECASE)
NOT_DOC = re.compile(r"검토|평가|기업분석|회사분석|공공자료|회사사실|결과|연봉|결정|예상질문|문항|기록|배정표|작성기록")
ESSAY_NAME = re.compile(r"자소서|자기소개|(지원서|문항).*초안")
ESSAY_EXEMPT = re.compile(r"검토\.md$|[-_]검토[-_]|검토결과|예상질문|글자수|메모|백업|작성기록|배정표|^(?!.*(초안|자소서|자기소개)).*문항")
ESSAY_EXT = re.compile(r"\.(md|docx|txt|hwp|hwpx)$", re.IGNORECASE)
FILLED = re.compile(r"[^\W_]")

LOG_NAME = "작성기록.md"
ASSIGN_NAME = "배정표.md"
HEADER = ["단계", "산출물", "찾아본 곳", "질문과 답", "생략 승인", "작성", "확인"]
STEPS = "①②③④⑤⑥⑦⑧⑨⑩"
REQUIRED = "①②③④"
PATCH_FILE = re.compile(r"^\*\*\* (?:Add File|Update File|Move to): (.+)$", re.MULTILINE)
PASS_LINE = re.compile(r"^\s*-\s*\d{4}-\d{2}-\d{2}\s+(.+?):\s*「")

MESSAGE = (
    "job-search-kr: 지원 서류 파일을 쓰려고 합니다. SKILL.md 「서류 문구의 필수 과정」 ①~③"
    "(지원 가능 확인, 회사·직무 재료 확인, 경험 재료 확인)을 이번 작업에서 마쳤는지 확인하고, 빠진 것이 있으면 지금 채웁니다. "
    "구직 사이트 글이면 site-profile.md 「순서」의 1~3번을 마쳤는지 확인합니다. "
    "사용자에게 보이기 전에 job-reviewer 검토를 거칩니다."
)
LEGACY_MESSAGE = (
    "job-search-kr: 저장 가드 도입 전에 만든 지원 폴더라서 막지 않습니다. 그래도 SKILL.md 「서류 문구의 필수 과정」 ①~③을 "
    "마쳤는지 확인하고, 사용자에게 보이기 전에 job-reviewer 검토를 거칩니다."
)
DRAFT_OWNER_MESSAGE = (
    "자소서 초안 파일은 job-writer만 저장합니다. job-writer에게 맡기세요. "
    "메인 세션이 백업을 남길 때는 초안 파일을 쓰지 말고 Bash로 복사합니다."
)
REMINDER = (
    "job-search-kr: 작성기록 ①~④ 확인을 마쳤습니다. 저장한 뒤 메인 대화가 ⑥ 작문 심사와 칸 재작성, ⑦ 사실·합격 심사, ⑧ 재심사와 마감을 "
    "job-reviewer로 거치고 작성기록.md 에 적습니다. 사용자에게 보이기 전에 끝냅니다."
)


def normalize(path):
    return unicodedata.normalize("NFC", path.replace("\\", "/"))


def is_filled(value) -> bool:
    """칸에 글자·숫자·한글이 하나라도 있어야 채운 것으로 본다(`-`, `—`, `.` 는 빈 칸)."""
    return bool(FILLED.search(value or ""))


def workspace_split(path: str):
    """작업 폴더 아래 applications/ 나 work/ 안의 경로면 (작업 폴더, 'applications'|'work', 나머지 조각들), 아니면 None.

    작업 폴더는 `profile/profile.md`가 있는 폴더다. 여러 후보가 있으면 가장 안쪽 것을 쓴다."""
    path = normalize(path)
    for m in reversed(list(re.finditer(r"/(applications|work)/", path))):
        root = path[:m.start()]
        if root and (Path(root) / "profile" / "profile.md").is_file():
            return root, m.group(1), path[m.end():].split("/")
    return None


def is_essay(path: str) -> bool:
    """applications/<폴더>/ 아래의 자소서 파일. 검토 보고서·작성기록·배정표·문항 파일은 아니다."""
    split = workspace_split(path)
    if split is None or split[1] != "applications":
        return False
    parts = split[2]
    if len(parts) < 2 or not parts[0]:
        return False
    name = parts[-1]
    return bool(ESSAY_NAME.search(name)) and bool(ESSAY_EXT.search(name)) and not ESSAY_EXEMPT.search(name)


def is_document(path: str) -> bool:
    if workspace_split(path) is None:
        return False
    if is_essay(path):
        return True
    name = normalize(path).rsplit("/", 1)[-1]
    return bool(DOC_NAME.search(name)) and not NOT_DOC.search(name)


def essay_location(path):
    """자소서 파일이면 (지원 폴더 경로, 폴더 이름, 작업 폴더), 아니면 None."""
    if not is_essay(path):
        return None
    root, _, parts = workspace_split(path)
    return f"{root}/applications/{parts[0]}", parts[0], root


def split_row(line):
    line = line.strip()
    if not line.startswith("|"):
        return None
    cells = line.split("|")[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    cells = [c.strip() for c in cells]
    if len(cells) == len(HEADER) - 1:  # 끝 칸이 비어 있고 맺음 `|` 도 없는 줄
        cells.append("")
    return cells


def parse_log(text):
    """작성기록 표를 {단계기호: {열이름: 값}} 로 돌려준다. 머리글이 없으면 None."""
    rows, in_table = {}, False
    for line in text.splitlines():
        cells = split_row(line)
        if cells is None:
            if in_table and rows:
                break
            continue
        if not in_table:
            if cells == HEADER:
                in_table = True
            continue
        if cells and cells[0] and cells[0][0] in STEPS and len(cells) == len(HEADER):
            rows[cells[0][0]] = dict(zip(HEADER, cells))
    return rows if in_table else None


def user_pass_names(text):
    names, in_section = set(), False
    for line in text.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## 사용자 통과"
            continue
        if in_section:
            m = PASS_LINE.match(line)
            if m:
                names.add(unicodedata.normalize("NFC", m.group(1).strip()))
    return names


def legacy_folders(root):
    """작업 폴더의 work/legacy-folders.txt 에 적은 옛 지원 폴더 이름(한 줄에 하나, # 은 주석)."""
    try:
        with open(os.path.join(root, "work", "legacy-folders.txt"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return set()
    return {unicodedata.normalize("NFC", s.strip()) for s in lines if s.strip() and not s.strip().startswith("#")}


def emit(key, value, deny=False):
    out = {"hookEventName": "PreToolUse"}
    if deny:
        out["permissionDecision"] = "deny"
        out["permissionDecisionReason"] = value
    else:
        out[key] = value
    print(json.dumps({"hookSpecificOutput": out}, ensure_ascii=False))


def deny(reason):
    emit(None, reason, deny=True)


def guide(message):
    emit("additionalContext", message)


def check_essay(path, folder_path, folder, root, agent_type, check_owner=True):
    """check_owner: 초안을 job-writer만 저장하는지 본다. 에이전트 이름이 오지 않는 Codex 입력에서는 False."""
    name = normalize(path).rsplit("/", 1)[-1]
    if folder in legacy_folders(root):
        # 옛 폴더는 작성기록을 요구하지 않지만, 초안은 여기서도 job-writer만 저장한다.
        if check_owner and "초안" in name and agent_type != "job-writer":
            deny(DRAFT_OWNER_MESSAGE)
            return
        guide(LEGACY_MESSAGE)
        return
    log_path = os.path.join(folder_path, LOG_NAME)
    if not os.path.isfile(log_path):
        deny(f"applications/{folder}/{LOG_NAME} 이 없습니다. references/writing-log.md 양식으로 만들고 "
             "필수 과정 ①~④를 먼저 마친 뒤 자소서를 저장합니다.")
        return
    try:
        with open(log_path, encoding="utf-8") as f:
            text = f.read()
    except UnicodeDecodeError:
        deny(f"{LOG_NAME} 을 UTF-8로 읽지 못했습니다. 기록은 UTF-8로 저장해야 합니다. UTF-8로 다시 저장한 뒤 저장합니다.")
        return
    rows = parse_log(text)
    if rows is None or any(s not in rows for s in REQUIRED):
        deny(f"{LOG_NAME} 이 references/writing-log.md 양식을 따르지 않습니다(표 머리글이나 ①~④ 줄이 없습니다). "
             "양식대로 고친 뒤 저장합니다.")
        return
    if name in user_pass_names(text):
        guide(f"job-search-kr: {LOG_NAME} 「사용자 통과」에 적힌 사용자 승인으로 {name} 저장을 허용합니다.")
        return
    if check_owner and "초안" in name and agent_type != "job-writer":
        deny(DRAFT_OWNER_MESSAGE)
        return
    missing = []
    for s in REQUIRED:
        if not is_filled(rows[s]["산출물"]) and not is_filled(rows[s]["생략 승인"]):
            missing.append(f"{s} {rows[s]['단계'][1:].strip()} (산출물 또는 생략 승인이 비어 있음)")
    if not is_filled(rows["④"]["생략 승인"]) and not os.path.isfile(os.path.join(folder_path, ASSIGN_NAME)):
        missing.append(f"④ 칸 설계 ({ASSIGN_NAME} 파일이 없음)")
    if missing:
        deny("자소서를 저장하기 전에 끝내야 할 과정이 비어 있습니다: " + "; ".join(missing)
             + f". {LOG_NAME} 에 적고 마친 뒤 저장합니다.")
        return
    guide(REMINDER)


def target_paths(data: dict) -> list:
    """Claude Code는 tool_input.file_path, Codex는 apply_patch 본문의 파일 줄에서 경로를 얻는다."""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    if isinstance(tool_input.get("file_path"), str) and tool_input["file_path"]:
        paths = [tool_input["file_path"]]
    elif isinstance(tool_input.get("command"), str):
        paths = PATCH_FILE.findall(tool_input["command"])
    else:
        return []
    cwd = data.get("cwd") or os.getcwd()
    return [os.path.normpath(os.path.join(cwd, p.strip())) for p in paths]


def main() -> None:
    try:
        data = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        if not isinstance(data, dict):
            return
        paths = target_paths(data)
        agent_type = data.get("agent_type")
        is_patch = not (data.get("tool_input") or {}).get("file_path")
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return
    try:
        docs = [p for p in paths if is_document(p)]
        if not docs:
            return
        for path in docs:
            loc = essay_location(path)
            if loc is not None:
                check_essay(path, loc[0], loc[1], loc[2], agent_type, check_owner=not is_patch)
                return
        guide(MESSAGE)
    except Exception as exc:  # fail open
        print(f"doc_write_check: 예상하지 못한 오류로 안내만 합니다: {exc}", file=sys.stderr)
        guide(MESSAGE)


if __name__ == "__main__":
    main()
