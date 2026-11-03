#!/usr/bin/env python3
"""forms/ 폴더의 회사 양식(.docx, .hwpx, .hwp)을 읽고 채운다.

  form_tool.py inspect  → work/form-fields.json (문단과 표 칸 목록)
  form_tool.py fill     → work/form-answers.json 을 읽어 applications/<폴더>/<원래 이름>-작성.docx|.hwpx|.hwp

인자는 inspect / fill 하나만 받는다. 파일 경로는 받지 않는다.
양식에 외부 연결·매크로·숨긴 글자가 있으면 막지 않고 알린다.
"""
import copy
import io
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

import hwp
from _common import ROOT, WORK, UserError, atomic_write, clean_text, load_json, output_dir, sanitize_name, unique_path

FORMS = ROOT / "forms"
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
MAX_FILE = 20 * 1024 * 1024
MAX_UNZIPPED = 200 * 1024 * 1024
MAX_ENTRIES = 3000
MAX_ANSWER = 20000
MAX_PHOTO = 10 * 1024 * 1024


def w(tag):
    return f"{{{W}}}{tag}"


# ---------- 파일 찾기 ----------

def find_form():
    if FORMS.is_symlink() or not FORMS.is_dir():
        raise UserError("forms 폴더가 없습니다.")
    files = [p for p in FORMS.iterdir() if not p.name.startswith(".")]
    if not files:
        raise UserError("forms 폴더가 비어 있습니다. 회사 양식 파일을 하나 넣어 주세요.")
    if len(files) > 1:
        raise UserError(f"forms 폴더에 파일이 {len(files)}개 있습니다. 채울 양식 하나만 남겨 주세요.")
    path = files[0]
    if path.is_symlink() or not path.is_file():
        raise UserError("forms 안의 항목이 일반 파일이 아닙니다.")
    ext = path.suffix.lower()
    if ext not in (".docx", ".hwpx", ".hwp"):
        raise UserError(f"{ext} 형식은 채우지 못합니다. 칸별 답을 텍스트로 드리겠습니다.")
    if path.stat().st_size > MAX_FILE:
        raise UserError("양식 파일이 20MB를 넘습니다.")
    return path


def open_package(path):
    kind = path.suffix.lower()[1:]
    program = "Word" if kind == "docx" else "한글"
    try:
        z = zipfile.ZipFile(path)
    except zipfile.BadZipFile:
        raise UserError(f"올바른 {program} 파일이 아닙니다.")
    infos = z.infolist()
    if len(infos) > MAX_ENTRIES or sum(i.file_size for i in infos) > MAX_UNZIPPED:
        raise UserError("압축을 풀면 너무 큰 파일이라 열지 않습니다.")
    # 다른 압축 방식은 파이썬이 한도 없이 푼다. Word와 한글은 이 둘만 쓴다.
    if any(i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for i in infos):
        raise UserError(f"{program}이 쓰지 않는 압축 방식이 들어 있어 열지 않습니다.")
    if kind == "docx" and "word/document.xml" not in z.namelist():
        raise UserError("Word 본문(word/document.xml)이 없습니다.")
    if kind == "hwpx":
        if "mimetype" not in z.namelist() or read_member(z, "mimetype").strip() != b"application/hwp+zip":
            raise UserError("한글 파일 표시(mimetype)가 없거나 달라 .hwpx 로 읽지 않습니다.")
        if not hwpx_sections(z):
            raise UserError("한글 본문(Contents/section0.xml)이 없습니다.")
    return z


def read_member(z, member):
    """항목 하나를 1MB씩 읽는다. 선언된 크기까지만 풀리므로 메모리는 위의 선언 크기 합계를 넘지 않는다.
    한 번에 전부 읽으면 크기를 속인 항목을 끝까지 푼 뒤에야 자른다."""
    parts = []
    try:
        with z.open(member) as f:
            while chunk := f.read(1 << 20):
                parts.append(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError):
        raise UserError("양식 파일이 손상됐거나 압축 정보가 맞지 않습니다. Word나 한글에서 열어 다시 저장한 뒤 넣어 주세요.")
    return b"".join(parts)


# ---------- XML 읽기·쓰기 (접두어와 이름공간 선언 보존) ----------

def safe_xml(data, what):
    """DTD를 거부하고, 검사한 글자 그대로 읽는다(바이트로 넘기면 파서가 선언된 다른 인코딩으로 다시 읽는다)."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise UserError(f"{what}: UTF-8이 아니라 읽지 못합니다. Word나 한글에서 다시 저장해 넣어 주세요.")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise UserError(f"{what}: DTD 선언이 있어 열지 않습니다.")
    try:
        return text, ET.fromstring(text)
    except ET.ParseError:
        raise UserError(f"{what}: XML 형식이 올바르지 않습니다.")


def parse_document(data, what="양식 본문"):
    text, root = safe_xml(data, what)
    for _, (prefix, uri) in ET.iterparse(io.StringIO(text), events=("start-ns",)):
        if not re.fullmatch(r"ns\d+", prefix or ""):
            ET.register_namespace(prefix or "", uri)
    root_tag = re.search(r"<(?![?!])[^>]*>", text)
    if root_tag is None:
        raise UserError(f"{what}: 시작 태그를 찾지 못했습니다.")
    return root, root_tag.group(0)


def serialize(root, original_root_tag):
    out = ET.tostring(root, encoding="unicode")
    end = out.index(">")
    new_tag = out[: end + 1]
    declared = set(re.findall(r'xmlns(?::[\w.-]+)?="[^"]*"', new_tag))
    missing = [d for d in re.findall(r'xmlns(?::[\w.-]+)?="[^"]*"', original_root_tag) if d not in declared]
    if missing:
        close = "/>" if new_tag.endswith("/>") else ">"
        new_tag = new_tag[: -len(close)] + " " + " ".join(missing) + close
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n' + new_tag + out[end + 1:]).encode("utf-8")


# ---------- 문단과 표 칸 번호 매기기 ----------

def is_hidden(run):
    rpr = run.find(w("rPr"))
    vanish = rpr.find(w("vanish")) if rpr is not None else None
    return vanish is not None and vanish.get(w("val"), "true") not in ("0", "false", "off")


def text_of(elem, hidden=frozenset()):
    """글자를 모은다. hidden 에 든 글자(숨긴 글자)는 [숨긴 글자: …] 로 감싼다."""
    parts, inside = [], False
    for node in elem.iter():
        if node.tag == w("t") and node.text:
            if (node in hidden) != inside:
                inside = not inside
                parts.append("[숨긴 글자: " if inside else "]")
            parts.append(node.text)
        elif node.tag == w("tab"):
            parts.append("\t")
        elif node.tag in (w("br"), w("cr")) or (node.tag == w("p") and parts and parts[-1] != "\n"):
            if inside:
                parts.append("]")
                inside = False
            parts.append("\n")
    if inside:
        parts.append("]")
    return "".join(parts).strip("\n")


def body_blocks(body):
    """본문 바로 아래 문단과 표를 순서대로. 콘텐츠 컨트롤(sdt)은 안으로 들어간다."""
    for child in body:
        if child.tag == w("sdt"):
            content = child.find(w("sdtContent"))
            if content is not None:
                yield from body_blocks(content)
        elif child.tag in (w("p"), w("tbl")):
            yield child


def index_document(root):
    body = root.find(w("body"))
    if body is None:
        raise UserError("Word 본문을 찾지 못했습니다.")
    hidden = {t for r in body.iter(w("r")) if is_hidden(r) for t in r.iter(w("t"))}
    targets, paragraphs, tables = {}, [], []
    p_no = t_no = 0
    previous = ""
    for block in body_blocks(body):
        if block.tag == w("p"):
            pid = f"p{p_no}"
            p_no += 1
            text = text_of(block, hidden)
            targets[pid] = ("p", block)
            paragraphs.append({"id": pid, "text": text[:500], "empty": not text.strip(), "before": previous[:80]})
            if text.strip():
                previous = text.strip()
        else:
            tid = f"t{t_no}"
            t_no += 1
            rows = []
            for r_no, tr in enumerate(block.findall(w("tr"))):
                cells = []
                for c_no, tc in enumerate(tr.findall(w("tc"))):
                    cid = f"{tid}.r{r_no}.c{c_no}"
                    targets[cid] = ("c", tc)
                    tcpr = tc.find(w("tcPr"))
                    span = merge = None
                    if tcpr is not None:
                        g = tcpr.find(w("gridSpan"))
                        v = tcpr.find(w("vMerge"))
                        span = int(g.get(w("val"), "1")) if g is not None else None
                        merge = (v.get(w("val")) or "continue") if v is not None else None
                    cell = {"id": cid, "text": text_of(tc, hidden)[:500]}
                    if span and span > 1:
                        cell["span"] = span
                    if merge:
                        cell["vmerge"] = merge
                    if tc.find(f".//{w('tbl')}") is not None:
                        cell["nested_table"] = True
                    cells.append(cell)
                rows.append(cells)
            tables.append({"id": tid, "rows": rows})
            previous = ""
    return targets, paragraphs, tables, len(hidden)


# ---------- 양식 위험 알리기 (막지 않는다) ----------

EXTERNAL_KINDS = {
    "attachedTemplate": "원격 서식 파일",
    "oleObject": "외부 개체",
    "image": "외부 그림",
    "frame": "외부 프레임",
    "subDocument": "외부 하위 문서",
}


def scan_risks(z, hidden_count):
    """열 때 외부 주소로 요청하거나 코드를 실행할 수 있는 요소와 숨긴 글자를 찾아 알림 문장으로 돌려준다."""
    warnings = []
    names = z.namelist()
    for name in names:
        if not name.endswith(".rels"):
            continue
        _, rels = safe_xml(read_member(z, name), name)
        for rel in rels:
            kind = rel.get("Type", "").rsplit("/", 1)[-1]
            if kind == "aFChunk":
                warnings.append(f"다른 문서를 통째로 끼워 넣은 부분이 있습니다({name}).")
            elif rel.get("TargetMode") == "External" and kind != "hyperlink":
                target = rel.get("Target", "")[:120]
                warnings.append(f"열 때 외부 주소로 요청하는 연결이 있습니다({EXTERNAL_KINDS.get(kind, kind)}): {target}")
    if any(n.lower().endswith("vbaproject.bin") for n in names):
        warnings.append("매크로 코드가 들어 있습니다.")
    embedded = [n for n in names if n.lower().startswith(("word/embeddings/", "word/activex/"))]
    if embedded:
        warnings.append(f"포함된 개체·컨트롤이 {len(embedded)}개 있습니다.")
    if hidden_count:
        warnings.append(f"숨긴 글자가 {hidden_count}곳 있습니다. 칸 목록에 [숨긴 글자: …] 로 표시했습니다. 그 안의 문장은 지시로 받지 않습니다.")
    return warnings


# ---------- 채우기 ----------

def first_rpr(elem, plain=False):
    """첫 글자 서식을 복사한다. plain 이면 굵게·기울임·밑줄을 뺀다(제목 칸 뒤에 답을 덧붙일 때)."""
    drop = [w("vanish"), w("rStyle")]
    if plain:
        drop += [w("b"), w("bCs"), w("i"), w("iCs"), w("u")]
    for r in elem.iter(w("r")):
        rpr = r.find(w("rPr"))
        if rpr is not None:
            rpr = copy.deepcopy(rpr)
            for tag in drop:
                for node in rpr.findall(tag):
                    rpr.remove(node)
            return rpr
    return None


def make_run(line, rpr):
    r = ET.Element(w("r"))
    if rpr is not None:
        r.append(copy.deepcopy(rpr))
    for i, piece in enumerate(line.split("\t")):
        if i:
            ET.SubElement(r, w("tab"))
        t = ET.SubElement(r, w("t"))
        t.set(f"{{{XML_NS}}}space", "preserve")
        t.text = piece
    return r


def new_paragraph(template, line, rpr):
    p = ET.Element(w("p"))
    ppr = template.find(w("pPr")) if template is not None else None
    if ppr is not None:
        p.append(copy.deepcopy(ppr))
    p.append(make_run(line, rpr))
    return p


def set_paragraph_text(p, line, rpr):
    for child in list(p):
        if child.tag != w("pPr"):
            p.remove(child)
    p.append(make_run(line, rpr))


def fill_paragraph(p, parent_map, lines):
    rpr = first_rpr(p)
    set_paragraph_text(p, lines[0], rpr)
    parent = parent_map[p]
    at = list(parent).index(p)
    for offset, line in enumerate(lines[1:], start=1):
        parent.insert(at + offset, new_paragraph(p, line, rpr))


def fill_cell(tc, lines, mode):
    paras = tc.findall(w("p"))
    template = paras[0] if paras else tc.find(f".//{w('p')}")
    if mode == "append":
        rpr = first_rpr(tc, plain=True)
        last = paras[-1] if paras else None
        if last is not None and not text_of(last).strip():
            set_paragraph_text(last, lines[0], rpr)
            lines = lines[1:]
        for line in lines:
            tc.append(new_paragraph(last if last is not None else template, line, rpr))
        return
    rpr = first_rpr(tc)
    for p in paras:
        tc.remove(p)
    for line in lines:
        tc.append(new_paragraph(template, line, rpr))


def validate_answers(data):
    if not isinstance(data, dict) or not isinstance(data.get("answers"), list):
        raise UserError('work/form-answers.json 은 {"folder": ..., "answers": [...]} 형식이어야 합니다.')
    answers = []
    for i, item in enumerate(data["answers"]):
        if isinstance(item, dict) and "image" in item:  # 사진 항목은 validate_extras 가 본다
            continue
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("text"), str):
            raise UserError(f"answers[{i}] 에 id 와 text(문자열)가 있어야 합니다.")
        mode = item.get("mode", "replace")
        if mode not in ("replace", "append"):
            raise UserError(f"answers[{i}] 의 mode 는 replace 또는 append 여야 합니다.")
        if len(item["text"]) > MAX_ANSWER:
            raise UserError(f"answers[{i}] 의 글이 너무 깁니다.")
        answers.append((item["id"], clean_text(item["text"]), mode))
    return answers


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def validate_extras(data):
    """사진(answers 안의 image 항목)과 칸 높이(heights)의 모양을 검사해 (사진 목록, 높이 목록)을 돌려준다. 값의 범위는 hwp 쪽이 본다."""
    photos, heights = [], []
    for i, item in enumerate(data["answers"]):
        if not isinstance(item, dict) or "image" not in item:
            continue
        if "text" in item:
            raise UserError(f"answers[{i}] 에는 image 와 text 를 함께 쓸 수 없습니다.")
        if not isinstance(item.get("id"), str) or not isinstance(item["image"], str):
            raise UserError(f"answers[{i}] 에 id 와 image(사진 경로 문자열)가 있어야 합니다.")
        if not is_number(item.get("width_mm")) or not is_number(item.get("height_mm")):
            raise UserError(f"answers[{i}] 에 width_mm 와 height_mm(숫자)가 있어야 합니다.")
        photos.append(item)
    listed = data.get("heights", [])
    if not isinstance(listed, list):
        raise UserError('heights 는 [{"id": "t0.r24.c0", "height_mm": 120.0}, ...] 형식이어야 합니다.')
    for j, item in enumerate(listed):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not is_number(item.get("height_mm")):
            raise UserError(f"heights[{j}] 에 id(문자열)와 height_mm(숫자)가 있어야 합니다.")
        heights.append(item)
    return photos, heights


def read_photo(value):
    """사진 경로를 작업 폴더 아래의 실제 파일로만 받아 바이트를 읽는다. 심볼릭 링크·루트 밖·'..' 경로는 거부한다."""
    if not value or ".." in Path(value).parts:
        raise UserError("사진 경로가 올바르지 않습니다(.. 는 쓸 수 없습니다).")
    root = Path(os.path.abspath(ROOT))
    path = Path(os.path.abspath(ROOT / value))
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise UserError("사진은 작업 폴더 안의 파일만 쓸 수 있습니다.")
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise UserError("사진 경로에 심볼릭 링크가 있어 쓰지 않습니다.")
    if not parts or not path.is_file():
        raise UserError(f"사진 파일이 없습니다: {value}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise UserError("사진은 작업 폴더 안의 파일만 쓸 수 있습니다.")
    if path.stat().st_size > MAX_PHOTO:
        raise UserError("사진 파일이 10MB를 넘습니다.")
    return path.read_bytes()


# ---------- 한글(.hwpx) ----------
# 본문은 Contents/section*.xml 이다. 문단(p) 안의 run 에 글자(t)나 표(tbl)가 들어 있고,
# 표 칸(tc)은 subList 안에 문단을 가진다. 문단의 linesegarray 는 한글이 저장해 둔 줄 배치라서
# 글자를 바꾼 문단에서는 지워야 한글이 다시 계산한다. 남겨 두면 새 글자가 겹쳐 그려진다.
# 이름공간 판(2011, 2016)이 달라도 읽도록 태그는 이름공간을 뺀 이름으로 찾는다.

SECTION_RE = re.compile(r"Contents/section(\d+)\.xml")
FORM_OBJECTS = {"btn", "checkBtn", "radioBtn", "comboBox", "listBox", "edit", "scrollBar"}


def hwpx_sections(z):
    found = sorted((int(m.group(1)), n) for n in z.namelist() if (m := SECTION_RE.fullmatch(n)))
    return [n for _, n in found]


def local(elem):
    return elem.tag.rsplit("}", 1)[-1]


def same_ns(elem, name):
    return elem.tag[: len(elem.tag) - len(local(elem))] + name


def kids(elem, name):
    return [c for c in elem if local(c) == name]


def int_attr(elem, name, default=0):
    try:
        return int(elem.get(name, default))
    except ValueError:
        return default


def hwpx_text(elem, hidden=frozenset(), skip_tables=False):
    """글자를 모은다. 문단 사이는 줄바꿈. skip_tables 면 표 안 글자는 뺀다(표를 품은 문단의 제 글자만)."""
    parts, state = [], {"inside": False, "first": True}

    def walk(node):
        name = local(node)
        if skip_tables and name == "tbl":
            return
        if name == "p":
            if not state["first"]:
                parts.append("\n")
            state["first"] = False
        elif name == "t":
            if (node in hidden) != state["inside"]:
                state["inside"] = not state["inside"]
                parts.append("[숨긴 글자: " if state["inside"] else "]")
            if node.text:
                parts.append(node.text)
            for child in node:
                parts.append({"lineBreak": "\n", "tab": "\t"}.get(local(child), ""))
                if child.tail:
                    parts.append(child.tail)
            return
        for child in node:
            walk(child)

    try:
        walk(elem)
    except RecursionError:
        raise UserError("양식 구조가 너무 깊게 겹쳐 있어 읽지 않습니다.")
    if state["inside"]:
        parts.append("]")
    return "".join(parts).strip("\n")


def text_charpr(elem):
    """글자가 든 첫 run 의 글자 모양 번호. 없으면 첫 run, 그것도 없으면 0."""
    runs = [r for r in elem.iter() if local(r) == "run"]
    for run in runs:
        if kids(run, "t"):
            return run.get("charPrIDRef", "0")
    return runs[0].get("charPrIDRef", "0") if runs else "0"


class Hwpx:
    def __init__(self, z):
        self.z = z
        self.sections = []
        for name in hwpx_sections(z):
            root, tag = parse_document(read_member(z, name), name)
            self.sections.append((name, root, tag))
        roots = [root for _, root, _ in self.sections]
        self.hidden_blocks = [e for root in roots for e in root.iter() if local(e) == "hiddenComment"]
        self.hidden = {t for block in self.hidden_blocks for t in block.iter() if local(t) == "t"}
        self.used_ids = {e.get("id") for root in roots for e in root.iter() if local(e) == "p"}
        numeric = [int(i) for i in self.used_ids if i and i.isdigit()]
        self.next_id = max(numeric, default=0) + 1
        self.header = self.header_tag = None
        self.header_changed = False
        self.touched = set()

    # 읽기

    def index(self):
        targets, paragraphs, tables = {}, [], []
        p_no = t_no = 0
        previous = ""
        for _, root, _ in self.sections:
            for p in kids(root, "p"):
                own = [tbl for run in kids(p, "run") for tbl in kids(run, "tbl")]
                text = hwpx_text(p, self.hidden, skip_tables=True)
                if text.strip() or not own:
                    pid = f"p{p_no}"
                    p_no += 1
                    targets[pid] = ("p", p, root)
                    paragraphs.append({"id": pid, "text": text[:500], "empty": not text.strip(), "before": previous[:80]})
                    if text.strip():
                        previous = text.strip()
                for tbl in own:
                    tid = f"t{t_no}"
                    t_no += 1
                    rows = []
                    for r_no, tr in enumerate(kids(tbl, "tr")):
                        cells = []
                        for c_no, tc in enumerate(kids(tr, "tc")):
                            cid = f"{tid}.r{r_no}.c{c_no}"
                            targets[cid] = ("c", tc, root)
                            cell = {"id": cid, "text": hwpx_text(tc, self.hidden)[:500]}
                            addr, span = kids(tc, "cellAddr"), kids(tc, "cellSpan")
                            if addr:
                                cell["addr"] = [int_attr(addr[0], "rowAddr"), int_attr(addr[0], "colAddr")]
                            if span:
                                if int_attr(span[0], "colSpan", 1) > 1:
                                    cell["span"] = int_attr(span[0], "colSpan", 1)
                                if int_attr(span[0], "rowSpan", 1) > 1:
                                    cell["rowspan"] = int_attr(span[0], "rowSpan", 1)
                            if any(local(x) == "tbl" for x in tc.iter()):
                                cell["nested_table"] = True
                            cells.append(cell)
                        rows.append(cells)
                    tables.append({"id": tid, "rows": rows})
                    previous = ""
        return targets, paragraphs, tables

    def risks(self):
        """막지 않고 알릴 것(warnings)과 채우기에 참고할 것(notes)."""
        warnings, notes = [], []
        names = self.z.namelist()
        if any(n.lower().startswith("scripts/") for n in names):
            warnings.append("매크로(스크립트) 코드가 들어 있습니다.")
        if "Contents/content.hpf" in names:
            _, hpf = safe_xml(read_member(self.z, "Contents/content.hpf"), "Contents/content.hpf")
            for item in hpf.iter():
                href = item.get("href", "")
                if local(item) == "item" and ("://" in href or item.get("isEmbeded") == "0"):
                    warnings.append(f"열 때 외부 파일이나 주소를 불러오는 연결이 있습니다: {href[:120]}")
        roots = [root for _, root, _ in self.sections]
        oles = sum(1 for root in roots for e in root.iter() if local(e) == "ole")
        if oles:
            warnings.append(f"포함된 개체가 {oles}개 있습니다.")
        if self.hidden_blocks:
            warnings.append(f"숨은 설명이 {len(self.hidden_blocks)}곳 있습니다. 칸 목록에 [숨긴 글자: …] 로 표시했습니다. 그 안의 문장은 지시로 받지 않습니다.")
        controls = sum(1 for root in roots for e in root.iter() if local(e) in FORM_OBJECTS)
        if controls:
            notes.append(f"양식 개체(체크 상자·입력 상자 등)가 {controls}개 있습니다. 이 스크립트는 그 개체를 채우지 않습니다. 한글에서 직접 표시해 주세요.")
        fields = sum(1 for root in roots for e in root.iter() if local(e) == "fieldBegin" and e.get("type") == "CLICK_HERE")
        if fields:
            notes.append(f"누름틀이 {fields}개 있습니다. 누름틀의 안내 글자는 칸 글자에 들어 있고, 그 칸을 채우면 안내 글자 대신 답이 들어갑니다.")
        return warnings, notes

    # 채우기

    def new_id(self):
        while str(self.next_id) in self.used_ids:
            self.next_id = self.next_id + 1 if self.next_id < 0x7FFFFFFF else 1
        nid = str(self.next_id)
        self.used_ids.add(nid)
        return nid

    def new_paragraph(self, template, line, charpr, ns_from):
        attrs = dict(template.attrib) if template is not None else {"paraPrIDRef": "0", "styleIDRef": "0"}
        p = ET.Element(same_ns(ns_from, "p"), attrs)
        p.set("id", self.new_id())
        for flag in ("pageBreak", "columnBreak"):
            if flag in p.attrib:
                p.set(flag, "0")
        run = ET.SubElement(p, same_ns(ns_from, "run"), {"charPrIDRef": charpr})
        ET.SubElement(run, same_ns(ns_from, "t")).text = line
        return p

    def set_text(self, p, line, charpr, force_charpr=False):
        """문단의 글자만 바꾼다. 표·그림·구역 설정처럼 글자가 아닌 것은 남긴다."""
        for cache in kids(p, "linesegarray"):
            p.remove(cache)
        target = None
        for run in kids(p, "run"):
            texts = kids(run, "t")
            if texts and target is None:
                target = run
            for t in texts:
                run.remove(t)
            if texts and run is not target and len(run) == 0:
                p.remove(run)
        if target is None:
            target = ET.SubElement(p, same_ns(p, "run"), {"charPrIDRef": charpr})
        elif force_charpr:
            target.set("charPrIDRef", charpr)
        ET.SubElement(target, same_ns(p, "t")).text = line

    def fill_paragraph(self, p, root, lines):
        charpr = text_charpr(p)
        self.set_text(p, lines[0], charpr)
        at = list(root).index(p)
        for offset, line in enumerate(lines[1:], start=1):
            root.insert(at + offset, self.new_paragraph(p, line, charpr, p))

    def fill_cell(self, tc, lines, mode):
        subs = kids(tc, "subList")
        if not subs:
            return False
        sub = subs[0]
        # 글자를 눌러 담는 칸(SQUEEZE)은 긴 답이 겹쳐 그려진다. 줄을 바꾸게 한다.
        if sub.get("lineWrap") == "SQUEEZE":
            sub.set("lineWrap", "BREAK")
        paras = kids(sub, "p")
        charpr = text_charpr(tc)
        if mode == "append":
            charpr = self.plain_charpr(charpr)
            last = paras[-1] if paras else None
            if last is not None and not hwpx_text(last).strip() and not any(local(x) == "tbl" for x in last.iter()):
                self.set_text(last, lines[0], charpr, force_charpr=True)
                lines = lines[1:]
            for line in lines:
                sub.append(self.new_paragraph(last, line, charpr, sub))
            return True
        template = paras[0] if paras else None
        for p in paras:
            sub.remove(p)
        for line in lines:
            sub.append(self.new_paragraph(template, line, charpr, sub))
        return True

    def plain_charpr(self, cid):
        """cid 글자 모양에서 굵게·기울임·밑줄을 뺀 모양의 번호. 없으면 header.xml 에 하나 더한다."""
        if self.header is None:
            if "Contents/header.xml" not in self.z.namelist():
                return cid
            self.header, self.header_tag = parse_document(read_member(self.z, "Contents/header.xml"), "Contents/header.xml")
        props = next((e for e in self.header.iter() if local(e) == "charProperties"), None)
        if props is None:
            return cid
        chars = kids(props, "charPr")
        source = next((c for c in chars if c.get("id") == cid), None)
        if source is None:
            return cid
        plain = copy.deepcopy(source)
        for child in list(plain):
            if local(child) in ("bold", "italic"):
                plain.remove(child)
            elif local(child) == "underline":
                child.set("type", "NONE")

        def signature(elem):
            elem = copy.deepcopy(elem)
            elem.attrib.pop("id", None)
            return ET.tostring(elem)

        wanted = signature(plain)
        for c in [source] + chars:
            if signature(c) == wanted:
                return c.get("id")
        ids = [int(c.get("id")) for c in chars if (c.get("id") or "").isdigit()]
        plain.set("id", str(max(ids, default=-1) + 1))
        props.append(plain)
        props.set("itemCnt", str(len(chars) + 1))
        self.header_changed = True
        return plain.get("id")


# ---------- 명령 ----------

def read_docx(z):
    root, _ = parse_document(read_member(z, "word/document.xml"))
    _, paragraphs, tables, hidden_count = index_document(root)
    return paragraphs, tables, scan_risks(z, hidden_count), []


def read_hwpx(z):
    doc = Hwpx(z)
    _, paragraphs, tables = doc.index()
    warnings, notes = doc.risks()
    return paragraphs, tables, warnings, notes


def fill_docx(z, answers):
    root, root_tag = parse_document(read_member(z, "word/document.xml"))
    targets, _, _, hidden_count = index_document(root)
    warnings = scan_risks(z, hidden_count)
    parent_map = {child: parent for parent in root.iter() for child in parent}
    applied, unknown = [], []
    for aid, text, mode in answers:
        if aid not in targets:
            unknown.append(aid)
            continue
        kind, elem = targets[aid]
        lines = text.split("\n") or [""]
        if kind == "p":
            fill_paragraph(elem, parent_map, lines)
        else:
            fill_cell(elem, lines, mode)
        applied.append(aid)
    return {"word/document.xml": serialize(root, root_tag)}, applied, unknown, warnings


def fill_hwpx(z, answers):
    doc = Hwpx(z)
    targets, _, _ = doc.index()
    warnings, _ = doc.risks()
    applied, unknown, touched = [], [], set()
    for aid, text, mode in answers:
        if aid not in targets:
            unknown.append(aid)
            continue
        kind, elem, root = targets[aid]
        # 한글의 탭은 탭 설정을 함께 적어야 해서 띄어쓰기로 바꾼다.
        lines = text.replace("\t", " ").split("\n")
        if kind == "p":
            doc.fill_paragraph(elem, root, lines)
        elif not doc.fill_cell(elem, lines, mode):
            unknown.append(aid)
            continue
        touched.add(id(root))
        applied.append(aid)
    replaced = {name: serialize(root, tag) for name, root, tag in doc.sections if id(root) in touched}
    if doc.header_changed:
        replaced["Contents/header.xml"] = serialize(doc.header, doc.header_tag)
    return replaced, applied, unknown, warnings


def inspect():
    path = find_form()
    kind = path.suffix.lower()[1:]
    if kind == "hwp":
        paragraphs, tables, warnings, notes = hwp.read(path.read_bytes())
    else:
        with open_package(path) as z:
            paragraphs, tables, warnings, notes = (read_docx if kind == "docx" else read_hwpx)(z)
    result = {"file": path.name, "type": kind, "warnings": warnings, "notes": notes,
              "paragraphs": paragraphs, "tables": tables}
    WORK.mkdir(exist_ok=True)
    out = WORK / "form-fields.json"
    if out.is_symlink():
        raise UserError("work/form-fields.json 이 심볼릭 링크라서 쓰지 않습니다.")
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    cells = sum(len(r) for t in tables for r in t["rows"])
    empty = sum(1 for p in paragraphs if p["empty"])
    print(f"양식: {path.name}")
    print(f"본문 문단 {len(paragraphs)}개(빈 문단 {empty}개), 표 {len(tables)}개(칸 {cells}개)")
    print("자세한 목록: work/form-fields.json")
    if any(c.get("nested_table") for t in tables for r in t["rows"] for c in r):
        tail = "안쪽 표도 문서에 나온 순서대로 표 번호를 받습니다." if kind == "hwp" else "안쪽 표는 칸 번호가 따로 매겨지지 않습니다."
        print(f"주의: 표 안에 표가 있는 칸이 있습니다. {tail}")
    for message in warnings:
        print(f"주의: {message}")
    for message in notes:
        print(f"참고: {message}")
    return 0


def fill():
    path = find_form()
    kind = path.suffix.lower()[1:]
    data = load_json(WORK / "form-answers.json")
    answers = validate_answers(data)
    photos, heights = validate_extras(data)
    target_dir = output_dir(data.get("folder"))
    changed = preview = preview_error = None
    if kind == "hwp":
        raw = path.read_bytes()
        if len(photos) > 1:
            raise UserError("사진은 한 번에 한 장만 넣을 수 있습니다.")
        photo = None
        if photos:
            photo = {"id": photos[0]["id"], "data": read_photo(photos[0]["image"]),
                     "width_mm": photos[0]["width_mm"], "height_mm": photos[0]["height_mm"]}
        new_raw, applied, unknown, warnings, changed = hwp.fill(raw, answers, [(h["id"], h["height_mm"]) for h in heights], photo)
        out = unique_path(target_dir, sanitize_name(path.stem, "양식") + "-작성", ".hwp")
        with atomic_write(out) as tmp:
            tmp.write_bytes(new_raw)
        try:
            failed = hwp.verify(raw, out.read_bytes(), changed)
        except Exception as exc:  # 어떤 예외든 검수 실패로 보고 결과 파일을 남기지 않는다
            out.unlink(missing_ok=True)
            raise UserError(f"자체 검수 중 오류: {type(exc).__name__}")
        if failed:
            out.unlink()
            raise UserError("만든 파일의 자체 검수에서 어긋난 항목이 있어 결과 파일을 지웠습니다. " + " / ".join(failed))
        try:  # 미리보기는 덤이다. 무슨 오류가 나도 .hwp 결과는 남기고 경고만 낸다.
            preview = unique_path(target_dir, out.stem + "-미리보기", ".html")
            with atomic_write(preview) as tmp:
                tmp.write_text(hwp.render_html(out.read_bytes()), encoding="utf-8")
        except Exception as exc:
            preview, preview_error = None, exc
    else:
        with open_package(path) as z:
            replaced, applied, unknown, warnings = (fill_docx if kind == "docx" else fill_hwpx)(z, answers)
            for xml in replaced.values():
                ET.fromstring(xml)  # 결과 XML이 올바른지 쓰기 전에 확인
            stem = sanitize_name(path.stem, "양식") + "-작성"
            out = unique_path(target_dir, stem, f".{kind}")
            with atomic_write(out) as tmp, zipfile.ZipFile(tmp, "w") as dst:
                # 순서와 압축 여부를 원본대로 둔다. 한글은 맨 앞 mimetype 이 압축되지 않아야 연다.
                for info in z.infolist():
                    payload = replaced.get(info.filename)
                    if payload is None:
                        payload = read_member(z, info)
                    keep = zipfile.ZIP_STORED if info.compress_type == zipfile.ZIP_STORED else zipfile.ZIP_DEFLATED
                    dst.writestr(info, payload, compress_type=keep)
        unknown = unknown + [f"{p['id']} (사진은 .hwp 양식에만 넣을 수 있음)" for p in photos]
        unknown += [f"{h['id']} (칸 높이 바꾸기는 .hwp 양식에만 쓸 수 있음)" for h in heights]
    print(f"만들었습니다: {out.relative_to(ROOT)}")
    print(f"채운 칸 {len(applied)}개")
    if unknown:
        print(f"없는 번호이거나 채울 수 없는 칸이라 건너뜀: {', '.join(unknown)}")
    for message in warnings:
        print(f"주의: {message} 결과 파일에도 그대로 남아 있습니다.")
    if changed is not None and changed.photo:
        print(f"사진: {changed.photo['id']} 칸에 넣었습니다({photos[0]['width_mm']}x{photos[0]['height_mm']}mm)")
    if changed is not None and changed.heights:
        print(f"칸 높이: {len(changed.heights['cells'])}개 칸을 바꿨습니다(표마다 행 높이 합계는 그대로)")
    if preview is not None:
        print(f"미리보기: {preview.relative_to(ROOT)} ({hwp.APPROX_NOTE})")
    if preview_error is not None:
        print(f"주의: 미리보기를 만들지 못했습니다({preview_error}). .hwp 결과는 그대로 남겨 두었습니다.")
    return 0


def main(argv):
    if len(argv) != 2 or argv[1] not in ("inspect", "fill"):
        print("사용법: form_tool.py inspect | fill")
        return 2
    return inspect() if argv[1] == "inspect" else fill()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except UserError as exc:
        print(f"오류: {exc}")
        sys.exit(1)
