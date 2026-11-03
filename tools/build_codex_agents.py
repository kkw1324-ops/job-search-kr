#!/usr/bin/env python3
"""Claude Code 에이전트 파일(agents/*.md)로 Codex 사용자 지정 에이전트(codex/agents/*.toml)를 만든다.

    python3 tools/build_codex_agents.py

에이전트 지침의 원본은 agents/*.md 하나다. 고친 뒤 이 스크립트를 다시 돌린다.
"""
import re
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "plugins" / "job-search-kr"
SRC = PLUGIN / "agents"
OUT = PLUGIN / "codex" / "agents"
HEADER = "# 자동 생성 파일: agents/{name}.md를 고친 뒤 tools/build_codex_agents.py로 다시 만든다.\n"


def parse(text: str):
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
    if not m:
        raise SystemExit("frontmatter가 없습니다")
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    return meta, m.group(2).strip() + "\n"


def toml_basic(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for md in sorted(SRC.glob("*.md")):
        meta, body = parse(md.read_text(encoding="utf-8"))
        if "'''" in body:
            raise SystemExit(f"{md.name}: 본문에 ''' 가 있어 TOML 리터럴 문자열로 넣을 수 없습니다")
        name = meta["name"]
        toml = (
            HEADER.format(name=name)
            + f"name = {toml_basic(name)}\n"
            + f"description = {toml_basic(meta['description'])}\n"
            + "developer_instructions = '''\n"
            + body
            + "'''\n"
        )
        (OUT / f"{name}.toml").write_text(toml, encoding="utf-8")
        print(f"wrote {OUT.relative_to(PLUGIN.parents[1])}/{name}.toml")


if __name__ == "__main__":
    main()
