"""옛 한글 형식(.hwp)을 읽고 채운다. 표준 라이브러리만 쓴다.

.hwp 는 OLE(CFB) 묶음 파일이다. 본문은 BodyText/Section0, Section1 … 스트림에 레코드로 들어 있다.
글자를 바꾼 문단의 PARA_HEADER·PARA_TEXT·PARA_CHAR_SHAPE 만 다시 쓰고 PARA_LINE_SEG(줄 배치 캐시)는 지워
한글이 열 때 다시 계산하게 한다. 바뀐 Section 스트림만 파일 끝의 새 섹터로 옮기고 나머지 바이트는 그대로 둔다.
글→칸 높이→사진 순서로 적용한다. 칸 높이는 LIST_HEADER 의 높이 값만 바꾸고, 사진은 같은 문서의 그림 레코드 형식으로
글자처럼 취급하는 그림을 넣는다(DocInfo 와 해당 Section 을 고치고 BinData 스트림을 덧붙임). render_html 은 근사 미리보기다.
"""
import base64
import hashlib
import html
import re
import struct
import zlib

from _common import UserError

SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
FREESECT, ENDOFCHAIN, FATSECT = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD
SPECIAL = 0xFFFFFFFA  # 이 값 이상은 섹터 번호가 아니라 표시
NOSTREAM = 0xFFFFFFFF
SS, MSS = 512, 64
PARA_HEADER, PARA_TEXT, PARA_CHAR_SHAPE, PARA_LINE_SEG, CTRL_HEADER, LIST_HEADER, TABLE = 66, 67, 68, 69, 71, 72, 77
PLAIN_KINDS = {PARA_TEXT, PARA_CHAR_SHAPE, PARA_LINE_SEG}
TEXT_KINDS = {PARA_HEADER, PARA_TEXT, PARA_CHAR_SHAPE, PARA_LINE_SEG}
TABLE_CTRL = b" lbt"
SECTION_RE = re.compile(r"^BodyText/Section(\d+)$")
PARA_ID_RE = re.compile(r"(t\d+\.r\d+\.c\d+)\.p\d+")
BAD_FILE = "한글 파일(.hwp)로 읽지 못했습니다. 한컴오피스에서 '다른 이름으로 저장'으로 .hwpx 나 .docx 로 바꿔 넣어 주세요."
MAX_RECORDS = 2_000_000
TOO_BIG = "양식 파일이 너무 크거나 손상되었습니다."
# 글자 하나가 16바이트(8글자 자리)를 차지하는 제어 문자: 확장(1~3, 11, 12, 14~18, 21~23), 인라인(4~9, 19, 20)
WIDE_CONTROLS = set(range(1, 10)) | {11, 12} | set(range(14, 24))
ID_MAPPINGS, BIN_DATA, DOC_CHAR_SHAPE, DOC_PARA_SHAPE, PAGE_DEF, SHAPE_COMPONENT, SHAPE_PICTURE = 17, 18, 21, 25, 73, 76, 85
HEIGHT_AT = 20  # LIST_HEADER 안의 칸 높이(4바이트) 위치
PHOTO_CELL_RE = re.compile(r"t\d+\.r\d+\.c\d+")


# ---------- OLE(CFB) 읽기 ----------

class Cfb:
    def __init__(self, data):
        self.data = data
        if len(data) < 512 or data[:8] != SIGNATURE:
            raise UserError(BAD_FILE)
        sector_shift, mini_shift = struct.unpack_from("<HH", data, 30)
        if sector_shift != 9 or mini_shift != 6:
            raise UserError("섹터 크기가 512바이트가 아닌 OLE 파일은 지원하지 않습니다.")
        nfat, dir_start, _, self.mini_cutoff, self.mini_start, _, _, ndifat = struct.unpack_from("<IIIIIIII", data, 44)
        self.dir_start = dir_start
        if ndifat:
            raise UserError("크기가 커서 DIFAT 섹터가 있는 한글 파일은 지원하지 않습니다.")
        if nfat > 109:
            raise UserError(BAD_FILE)
        self.difat = list(struct.unpack_from("<109I", data, 76))[:nfat]
        self.fat = []
        for s in self.difat:
            self.fat += struct.unpack("<%dI" % (SS // 4), self.sector(s))
        self.entries = []
        for s in self.chain(dir_start, self.fat):
            sec = self.sector(s)
            for k in range(SS // 128):
                self.entries.append(self.parse_entry(sec, k, s))
        if not self.entries or self.entries[0]["type"] != 5:
            raise UserError(BAD_FILE)
        self.mini_fat = []
        for s in self.chain(self.mini_start, self.fat):
            self.mini_fat += struct.unpack("<%dI" % (SS // 4), self.sector(s))
        root = self.entries[0]
        self.mini_stream = b"".join(self.sector(s) for s in self.chain(root["start"], self.fat))[:root["size"]]
        self.streams = {}
        try:
            self.walk(self.entries[0]["child"], "", set())
        except RecursionError:
            raise UserError(BAD_FILE)

    @staticmethod
    def offset(sector):
        return SS + sector * SS

    def sector(self, s):
        off = self.offset(s)
        if s >= SPECIAL or off + SS > len(self.data):
            raise UserError(BAD_FILE)
        return self.data[off:off + SS]

    def chain(self, start, table):
        out, seen = [], set()
        s = start
        while s < SPECIAL:
            if s in seen or s >= len(table):
                raise UserError(BAD_FILE)
            seen.add(s)
            out.append(s)
            s = table[s]
        return out

    def parse_entry(self, sec, k, sector):
        raw = sec[k * 128:(k + 1) * 128]
        name_len = struct.unpack_from("<H", raw, 64)[0]
        if name_len > 64:
            raise UserError(BAD_FILE)
        name = raw[:max(name_len - 2, 0)].decode("utf-16le", "replace")
        kind = raw[66]
        left, right, child = struct.unpack_from("<III", raw, 68)
        start, size = struct.unpack_from("<II", raw, 116)
        return {"name": name, "type": kind, "left": left, "right": right, "child": child, "start": start,
                "size": size, "pos": self.offset(sector) + k * 128}

    def walk(self, index, prefix, seen):
        if index == NOSTREAM:
            return
        if index >= len(self.entries) or index in seen:
            raise UserError(BAD_FILE)
        seen.add(index)
        e = self.entries[index]
        self.walk(e["left"], prefix, seen)
        path = prefix + e["name"]
        if e["type"] == 2:
            self.streams[path] = e
        elif e["type"] == 1:
            self.walk(e["child"], path + "/", seen)
        self.walk(e["right"], prefix, seen)

    def read(self, path):
        e = self.streams.get(path)
        if e is None:
            raise UserError(f"한글 파일에 {path} 가 없습니다.")
        if e["size"] < self.mini_cutoff:
            parts = []
            for s in self.chain(e["start"], self.mini_fat):
                off = s * MSS
                if off + MSS > len(self.mini_stream):
                    raise UserError(BAD_FILE)
                parts.append(self.mini_stream[off:off + MSS])
        else:
            parts = [self.sector(s) for s in self.chain(e["start"], self.fat)]
        data = b"".join(parts)[:e["size"]]
        if len(data) != e["size"]:
            raise UserError(BAD_FILE)
        return data


def grow_fat(b, fat, difat, wanted, what):
    """파일 끝에 붙일 섹터 wanted 개를 담을 칸이 FAT 에 모자라면 FAT 섹터를 파일 끝에 덧붙인다(b, fat, difat 를 직접 고친다)."""
    while len(fat) < (len(b) - SS) // SS + wanted:
        if len(difat) >= 109:
            raise UserError(f"한글 파일의 섹터 표(FAT)를 더 늘릴 수 없어 {what} 못합니다.")
        s = (len(b) - SS) // SS
        b += b"\0" * SS
        fat += [FREESECT] * (SS // 4)
        fat[s] = FATSECT
        difat.append(s)


def rebuild_ole(raw, path, new_data):
    """원본 바이트를 그대로 두고, 해당 스트림만 파일 끝의 새 섹터로 옮긴다."""
    cfb = Cfb(raw)
    entry = cfb.streams.get(path)
    if entry is None:
        raise UserError(f"한글 파일에 {path} 가 없습니다.")
    if len(new_data) < cfb.mini_cutoff:
        raise UserError(f"{path} 가 너무 작아(4096바이트 미만) 다시 쓰지 않습니다.")
    b = bytearray(raw)
    if (len(b) - SS) % SS:
        b += b"\0" * (SS - (len(b) - SS) % SS)
    fat = list(cfb.fat)
    # 옛 위치 비우기
    if entry["size"] < cfb.mini_cutoff:
        mini_fat = list(cfb.mini_fat)
        for ms in cfb.chain(entry["start"], mini_fat):
            mini_fat[ms] = FREESECT
        for n, s in enumerate(cfb.chain(cfb.mini_start, fat)):
            struct.pack_into("<%dI" % (SS // 4), b, Cfb.offset(s), *mini_fat[n * (SS // 4):(n + 1) * (SS // 4)])
    else:
        for s in cfb.chain(entry["start"], fat):
            fat[s] = FREESECT
    # 새 섹터를 파일 끝에 붙이기
    need = -(-len(new_data) // SS)
    difat = list(cfb.difat)
    grow_fat(b, fat, difat, need, "다시 쓰지")
    now = (len(b) - SS) // SS
    new_secs = list(range(now, now + need))
    if any(fat[a] != FREESECT for a in new_secs):
        raise UserError("한글 파일의 섹터 표(FAT)가 예상과 달라 다시 쓰지 못합니다.")
    for a, c in zip(new_secs, new_secs[1:] + [ENDOFCHAIN]):
        fat[a] = c
    b += new_data + b"\0" * (need * SS - len(new_data))
    write_table(b, difat, fat)
    if len(difat) != len(cfb.difat):
        struct.pack_into("<I", b, 44, len(difat))
        struct.pack_into("<109I", b, 76, *(difat + [FREESECT] * (109 - len(difat))))
    struct.pack_into("<II", b, entry["pos"] + 116, new_secs[0], len(new_data))
    return bytes(b)


def write_table(b, offsets, table):
    """섹터 번호 목록 offsets 에 해당하는 표(FAT 또는 미니 FAT)를 섹터마다 나눠 쓴다."""
    for n, s in enumerate(offsets):
        struct.pack_into("<%dI" % (SS // 4), b, Cfb.offset(s), *table[n * (SS // 4):(n + 1) * (SS // 4)])


def overwrite_mini(raw, path, data):
    """작은 스트림(미니 스트림)을 원래 미니 섹터 안에 덮어쓴다. 섹터 수가 늘면 UserError(호출한 쪽이 다른 방법을 쓴다)."""
    cfb = Cfb(raw)
    entry = cfb.streams.get(path)
    if entry is None or entry["size"] >= cfb.mini_cutoff:
        raise UserError(f"{path} 는 작은 스트림이 아닙니다.")
    chain = cfb.chain(entry["start"], cfb.mini_fat)
    need = max(-(-len(data) // MSS), 1)
    if len(data) >= cfb.mini_cutoff or need > len(chain):
        raise UserError(f"{path} 를 담을 미니 섹터가 모자랍니다.")
    root_chain = cfb.chain(cfb.entries[0]["start"], cfb.fat)
    b = bytearray(raw)
    padded = data + b"\0" * (need * MSS - len(data))
    for n, ms in enumerate(chain[:need]):
        pos = ms * MSS
        if pos // SS >= len(root_chain):
            raise UserError(BAD_FILE)
        o = Cfb.offset(root_chain[pos // SS]) + pos % SS
        b[o:o + MSS] = padded[n * MSS:(n + 1) * MSS]
    if need < len(chain):  # 줄어든 만큼 사슬 꼬리를 비운다
        mini_fat = list(cfb.mini_fat)
        mini_fat[chain[need - 1]] = ENDOFCHAIN
        for ms in chain[need:]:
            mini_fat[ms] = FREESECT
        write_table(b, cfb.chain(cfb.mini_start, cfb.fat), mini_fat)
    struct.pack_into("<I", b, entry["pos"] + 120, len(data))
    return bytes(b)


def add_stream(raw, storage, name, data, reserve=0):
    """저장소(storage) 안에 새 스트림을 파일 끝의 새 섹터로 덧붙인다. reserve 는 뒤이어 rebuild_ole 이 더 붙일 섹터 수.

    FAT 섹터가 모자라면 파일 끝에 FAT 섹터를 덧붙이고, 디렉터리 빈 항목이 없으면 디렉터리 섹터를 덧붙인다.
    저장소의 자식 트리는 '검정 뿌리 + 빨강 오른쪽 자식 하나'인 모양만 다룬다(그림 둘이 든 한글 양식의 모양).
    """
    cfb = Cfb(raw)
    if len(data) < cfb.mini_cutoff:
        raise UserError(f"{name} 가 너무 작아(4096바이트 미만) 새 스트림으로 넣지 않습니다.")
    if len(name) > 31:
        raise UserError("스트림 이름이 너무 깁니다.")
    parent = next((i for i, e in enumerate(cfb.entries) if e["type"] == 1 and e["name"] == storage), None)
    if parent is None:
        raise UserError(f"한글 파일에 {storage} 저장소가 없습니다.")
    b = bytearray(raw)
    if (len(b) - SS) % SS:
        b += b"\0" * (SS - (len(b) - SS) % SS)
    fat, difat = list(cfb.fat), list(cfb.difat)
    need = -(-len(data) // SS)
    grow_fat(b, fat, difat, need + 1 + reserve + 1, "사진을 넣지")  # 데이터 + 디렉터리 섹터 1 + 뒤이어 붙일 섹터 + 여유
    entries = list(cfb.entries)
    free = next((i for i, e in enumerate(entries) if e["type"] == 0), None)
    if free is None:
        dir_chain = cfb.chain(cfb.dir_start, fat)
        s = (len(b) - SS) // SS
        if fat[s] != FREESECT:
            raise UserError("한글 파일의 섹터 표(FAT)가 예상과 달라 사진을 넣지 못합니다.")
        b += b"\0" * SS
        fat[dir_chain[-1]], fat[s] = s, ENDOFCHAIN
        for k in range(SS // 128):  # 빈 항목은 형제·자식이 없다
            struct.pack_into("<III", b, Cfb.offset(s) + k * 128 + 68, NOSTREAM, NOSTREAM, NOSTREAM)
        free = len(entries)
        pos = Cfb.offset(s)
    else:
        pos = entries[free]["pos"]
    first = (len(b) - SS) // SS
    secs = list(range(first, first + need))
    if any(fat[a] != FREESECT for a in secs):
        raise UserError("한글 파일의 섹터 표(FAT)가 예상과 달라 사진을 넣지 못합니다.")
    for a, c in zip(secs, secs[1:] + [ENDOFCHAIN]):
        fat[a] = c
    b += data + b"\0" * (need * SS - len(data))
    encoded = name.encode("utf-16le") + b"\0\0"
    entry = bytearray(128)
    entry[:len(encoded)] = encoded
    struct.pack_into("<HBB", entry, 64, len(encoded), 2, 0)  # 스트림, 빨강
    struct.pack_into("<III", entry, 68, NOSTREAM, NOSTREAM, NOSTREAM)
    struct.pack_into("<IIi", entry, 116, secs[0], len(data), 0)
    b[pos:pos + 128] = entry
    # 저장소 자식 트리: 검정 뿌리(왼쪽 없음) + 빨강 오른쪽 자식이면, 그 자식을 뿌리로 올리고 옛 뿌리와 새 항목을 빨강 자식으로 둔다.
    key = lambda n: (len(n), n.upper())
    root = cfb.entries[parent]["child"]
    ok = root < len(entries) and entries[root]["left"] == NOSTREAM and entries[root]["right"] < len(entries) and b[entries[root]["pos"] + 67] == 1
    if ok:
        rc = entries[entries[root]["right"]]
        ok = (b[rc["pos"] + 67] == 0 and rc["left"] == NOSTREAM and rc["right"] == NOSTREAM
              and key(entries[root]["name"]) < key(rc["name"]) < key(name))
    if not ok:
        raise UserError("BinData 안의 항목 배치가 예상과 달라 사진을 넣지 못합니다.")
    r_id, rc_id = root, entries[root]["right"]
    struct.pack_into("<I", b, cfb.entries[parent]["pos"] + 76, rc_id)
    b[rc["pos"] + 67] = 1
    struct.pack_into("<II", b, rc["pos"] + 68, r_id, free)
    b[entries[r_id]["pos"] + 67] = 0
    struct.pack_into("<II", b, entries[r_id]["pos"] + 68, NOSTREAM, NOSTREAM)
    write_table(b, difat, fat)
    struct.pack_into("<I", b, 44, len(difat))
    struct.pack_into("<109I", b, 76, *(difat + [FREESECT] * (109 - len(difat))))
    return bytes(b)


# ---------- 레코드 ----------

def parse_records(d, limit=MAX_RECORDS):
    i, out = 0, []
    try:
        while i < len(d):
            if len(out) >= limit:
                raise UserError(TOO_BIG)
            h = struct.unpack_from("<I", d, i)[0]
            i += 4
            tag, lvl, size = h & 0x3ff, (h >> 10) & 0x3ff, (h >> 20) & 0xfff
            if size == 0xfff:
                size = struct.unpack_from("<I", d, i)[0]
                i += 4
            if i + size > len(d):
                raise struct.error("record out of range")
            out.append([tag, lvl, d[i:i + size]])
            i += size
    except struct.error:
        raise UserError("한글 본문 레코드를 읽지 못했습니다. 파일이 깨졌을 수 있습니다.")
    return out


def build_records(recs):
    out = bytearray()
    for tag, lvl, p in recs:
        if len(p) >= 0xfff:
            out += struct.pack("<II", tag | (lvl << 10) | (0xfff << 20), len(p))
        else:
            out += struct.pack("<I", tag | (lvl << 10) | (len(p) << 20))
        out += p
    return bytes(out)


def para_text(payload):
    """PARA_TEXT 내용을 글자로 바꾼다. 개체 표시 문자는 건너뛰고 탭은 \\t, 줄바꿈은 \\n 으로 둔다."""
    units = struct.unpack("<%dH" % (len(payload) // 2), payload[:len(payload) // 2 * 2])
    out, i = [], 0
    while i < len(units):
        u = units[i]
        if u in WIDE_CONTROLS:
            if u == 9:
                out.append("\t")
            i += 8
            continue
        i += 1
        if u == 10:
            out.append("\n")
        elif u in (24, 30, 31):
            out.append("-" if u == 24 else " ")
        elif u >= 32:
            out.append(chr(u))
    return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16", "replace")


# ---------- 문서 구조 ----------

class Para:
    """문단 하나. recs[i:end] 가 이 문단의 레코드(PARA_HEADER 와 그 아래)다. ordinal 은 구역 안의 문단 순서."""

    def __init__(self, recs, ordinal, i, end):
        self.ordinal, self.i, self.end = ordinal, i, end
        children = recs[i + 1:end]
        self.kinds = {c[0] for c in children}
        self.payload = next((c[2] for c in children if c[0] == PARA_TEXT and c[1] == recs[i][1] + 1), b"")
        self.text = para_text(self.payload)
        self.nchars = struct.unpack_from("<I", recs[i][2], 0)[0] & 0x7fffffff
        shape = next((c[2] for c in children if c[0] == PARA_CHAR_SHAPE), b"")
        # 글자만 든 문단(개체·누름틀 없음)이고 글자 수가 맞아야 안전하게 고칠 수 있다.
        self.plain = (self.kinds <= PLAIN_KINDS and len(shape) >= 8 and self.nchars == max(len(self.payload) // 2, 1)
                      and (not self.payload or self.payload[-2:] == b"\r\0"))


def scan_paragraphs(recs):
    """모든 PARA_HEADER 를 순서대로 Para 로 만든다(본문·칸·머리말·글상자 가리지 않음)."""
    ends, open_ = {}, []
    for k, (tag, lvl, _) in enumerate(recs):
        while open_ and recs[open_[-1]][1] >= lvl:
            ends[open_.pop()] = k
        if tag == PARA_HEADER:
            open_.append(k)
    for k in open_:
        ends[k] = len(recs)
    heads = [k for k, r in enumerate(recs) if r[0] == PARA_HEADER]
    for k in heads:
        if len(recs[k][2]) < 22:
            raise UserError("문단 정보를 읽지 못했습니다. 파일이 깨졌을 수 있습니다.")
    return {k: Para(recs, n, k, ends[k]) for n, k in enumerate(heads)}


def index_section(recs, table_start=0):
    """본문 문단(레벨 0)과 표를 문서에 나오는 순서대로 모은다.

    표는 머리말·꼬리말·글상자·다른 표 안에 있어도 나오는 순서대로 번호를 받는다(hwppatch 와 같은 방식).
    돌려주는 값: (본문 문단 목록, 표 목록). 표: {"no", "cells": {(행, 열): 칸}, "order": [(행, 열)…], "owner": 본문 문단 또는 None}
    칸: {"row", "col", "span", "rowspan", "level", "paras": [Para], "nested": bool, "head": LIST_HEADER 의 레코드 번호}
    """
    paras = scan_paragraphs(recs)
    body, tables, stack = [], [], []
    for k, (tag, lvl, p) in enumerate(recs):
        while stack and lvl <= stack[-1]["level"]:
            stack.pop()
        if tag == CTRL_HEADER and p[:4] == TABLE_CTRL:
            table = {"no": table_start + len(tables), "level": lvl, "cells": {}, "order": [], "seen_table": False,
                     "cur": None, "owner": body[-1] if body and not stack and lvl == 1 else None}
            if stack and stack[-1]["cur"] is not None:
                stack[-1]["cur"]["nested"] = True
            tables.append(table)
            stack.append(table)
        elif tag == TABLE and stack and lvl == stack[-1]["level"] + 1:
            stack[-1]["seen_table"] = True
        elif tag == LIST_HEADER and stack and stack[-1]["seen_table"] and lvl == stack[-1]["level"] + 1:
            if len(p) < 16:
                raise UserError("표 칸 정보를 읽지 못했습니다. 파일이 깨졌을 수 있습니다.")
            col, row, cspan, rspan = struct.unpack_from("<HHHH", p, 8)
            cell = {"row": row, "col": col, "span": cspan, "rowspan": rspan, "level": lvl, "paras": [], "nested": False, "head": k}
            if (row, col) in stack[-1]["cells"]:
                raise UserError("같은 위치에 칸이 둘 이상 있는 표라서 읽지 않습니다.")
            stack[-1]["cells"][(row, col)] = cell
            stack[-1]["order"].append((row, col))
            stack[-1]["cur"] = cell
        elif tag == PARA_HEADER:
            if lvl == 0:
                body.append(paras[k])
            elif stack and stack[-1]["cur"] is not None and lvl == stack[-1]["cur"]["level"]:
                stack[-1]["cur"]["paras"].append(paras[k])
    return body, tables


# ---------- 문서 열기 ----------

MAX_SECTION = 64 * 1024 * 1024  # 한 파일 안 모든 구역을 푼 크기의 합계 상한


def inflate(data, limit=MAX_SECTION):
    d = zlib.decompressobj(-15)
    try:
        out = d.decompress(data, limit + 1)
    except zlib.error:
        raise UserError(BAD_FILE)
    if len(out) > limit or not d.eof:
        raise UserError(TOO_BIG)
    return out


def deflate(data):
    """hwppatch 와 같은 규칙: 최대 압축, 결과가 4096바이트 미만이면 무압축 블록(작은 스트림 방지)."""
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    enc = c.compress(data) + c.flush()
    if len(enc) < 4096:
        c = zlib.compressobj(0, zlib.DEFLATED, -15)
        enc = c.compress(data) + c.flush()
    return enc


def is_compressed(cfb):
    header = cfb.read("FileHeader")
    if len(header) < 40 or not header.startswith(b"HWP Document File"):
        raise UserError(BAD_FILE)
    attrs = struct.unpack_from("<I", header, 36)[0]
    if attrs & 2:
        raise UserError("암호가 설정된 한글 문서는 지원하지 않습니다. 한글에서 암호를 푼 파일을 넣어 주세요.")
    if attrs & 4:
        raise UserError("배포용 문서는 지원하지 않습니다. 원본 문서를 넣어 주세요.")
    return bool(attrs & 1)


def load_sections(cfb):
    compressed = is_compressed(cfb)
    names = sorted((int(m.group(1)), n) for n in cfb.streams for m in [SECTION_RE.match(n)] if m)
    if not names:
        raise UserError("본문(BodyText)이 없는 한글 파일입니다.")
    out = []
    room, records = MAX_SECTION, MAX_RECORDS
    for _, name in names:
        data = cfb.read(name)
        if compressed:
            data = inflate(data, room)
        elif len(data) > room:
            raise UserError(TOO_BIG)
        room -= len(data)
        recs = parse_records(data, records)
        records -= len(recs)
        out.append((name, recs))
    return compressed, out


class HwpDoc:
    def __init__(self, raw):
        self.raw = raw
        self.cfb = Cfb(raw)
        self.compressed, loaded = load_sections(self.cfb)
        self.sections, self.targets, self.para_targets = [], {}, {}
        self.paragraphs, self.tables = [], []
        p_no = t_no = 0
        previous = ""
        for s_no, (name, recs) in enumerate(loaded):
            body, tables = index_section(recs, t_no)
            t_no += len(tables)
            self.sections.append({"name": name, "recs": recs})
            owners = {t["owner"].ordinal for t in tables if t["owner"] is not None}
            tables_by_owner = {}
            for t in tables:
                if t["owner"] is not None:
                    tables_by_owner.setdefault(t["owner"].ordinal, []).append(t)
            for para in body:
                if para.text.strip() or para.ordinal not in owners:
                    pid = f"p{p_no}"
                    p_no += 1
                    self.targets[pid] = ("p", s_no, para)
                    self.paragraphs.append({"id": pid, "text": para.text[:500], "empty": not para.text.strip(),
                                            "before": previous[:80]})
                    if para.text.strip():
                        previous = para.text.strip()
                if para.ordinal in owners:
                    previous = ""
            for t in tables:
                self.tables.append(self.table_json(t, s_no))

    def table_json(self, t, s_no):
        tid = f"t{t['no']}"
        rows = {}
        for key in t["order"]:
            c = t["cells"][key]
            cid = f"{tid}.r{c['row']}.c{c['col']}"
            self.targets[cid] = ("c", s_no, c)
            cell = {"id": cid, "text": "\n".join(p.text for p in c["paras"]).strip("\n")[:500], "addr": [c["row"], c["col"]]}
            if c["span"] > 1:
                cell["span"] = c["span"]
            if c["rowspan"] > 1:
                cell["rowspan"] = c["rowspan"]
            if c["nested"]:
                cell["nested_table"] = True
            for k, p in enumerate(c["paras"]):
                self.para_targets[f"{cid}.p{k}"] = (s_no, p)
            if len(c["paras"]) >= 2:
                cell["cell_paragraphs"] = [self.para_json(f"{cid}.p{k}", p) for k, p in enumerate(c["paras"])]
            rows.setdefault(c["row"], []).append(cell)
        return {"id": tid, "rows": [rows[r] for r in sorted(rows)]}


    @staticmethod
    def para_json(pid, para):
        item = {"id": pid, "text": para.text[:500], "empty": not para.text.strip()}
        if not para.plain:
            item["fillable"] = False
            item["skip"] = ("개체·그림·누름틀이 들어 있어 채울 수 없음" if not para.kinds <= PLAIN_KINDS
                            else "글자 구조가 달라 채울 수 없음")
        return item


def read(raw):
    doc = HwpDoc(raw)
    return doc.paragraphs, doc.tables, [], []


# ---------- 채우기 ----------

def normalize_text(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")


def encode_text(text):
    """문단 안 글자 코드 목록. 줄바꿈은 문단 안 줄바꿈(10)이다. 담을 수 없는 글자가 있으면 None."""
    if any(ord(c) > 0xFFFF or 0xD800 <= ord(c) <= 0xDFFF for c in text):
        return None
    return [10 if c == "\n" else ord(c) for c in text]


def pack_units(units):
    return struct.pack("<%dH" % len(units), *units)


def rewrite_para(recs, para, mode, text):
    """문단 하나의 PARA_HEADER·PARA_TEXT·PARA_CHAR_SHAPE 를 새로 만들고 PARA_LINE_SEG 는 지운다."""
    lvl = recs[para.i][1]
    head = bytearray(recs[para.i][2])
    shape = next(c[2] for c in recs[para.i + 1:para.end] if c[0] == PARA_CHAR_SHAPE)
    n_all, mask, ps, st, div, cs_cnt, rt, _, inst = struct.unpack_from("<IIHBBHHHI", head, 0)
    units = encode_text(text)
    if mode == "replace":
        payload = pack_units(units + [13]) if units else b""
        count = len(units) + 1
        mask = (1 << 10) if 10 in units else 0
        cs_cnt, shape = 1, shape[:8]
    else:
        base = para.payload[:-2]
        payload = base + pack_units(units + [13])
        count = len(base) // 2 + len(units) + 1
        if 10 in units:
            mask |= 1 << 10
    struct.pack_into("<IIHBBHHHI", head, 0, (n_all & 0x80000000) | count, mask, ps, st, div, cs_cnt, rt, 0, inst)
    out = [[PARA_HEADER, lvl, bytes(head)]]
    if payload:
        out.append([PARA_TEXT, lvl + 1, payload])
    out.append([PARA_CHAR_SHAPE, lvl + 1, shape])
    return out


def plan_cell(cell, mode, text, current):
    """칸에 쓸 문단 수정 목록 [(문단, 방식, 글자)]. 채울 수 없으면 None."""
    paras = cell["paras"]
    if not paras:
        return None
    if mode == "append":
        last = paras[-1]
        if not last.plain:
            return None
        if not text:
            return []
        return [(last, "append", ("\n" if current(last).strip() else "") + text)]
    if not paras[0].plain:
        return None
    steps = [(paras[0], "replace", text)]
    for para in paras[1:]:
        if current(para):
            if not para.plain:
                return None
            steps.append((para, "replace", ""))
    return steps


def plan_para(para, mode, text, current):
    """칸 안의 문단 하나에 쓸 문단 수정 목록. 채울 수 없으면 None."""
    if not para.plain:
        return None
    if mode == "append":
        if not text:
            return []
        return [(para, "append", ("\n" if current(para).strip() else "") + text)]
    return [(para, "replace", text)]


class Changed(dict):
    """구역 스트림 이름 -> {"touched", "expected"}. 칸 높이·사진을 적용했을 때만 heights·photo 가 채워진다."""
    heights = None
    photo = None


def fill(raw, answers, heights=(), photo=None):
    """답을 채운 새 파일 바이트와 (적용, 건너뜀, 경고, 검수 계획)을 돌려준다.

    heights: [(칸 번호, 높이 mm)], photo: {"id", "data"(JPEG·PNG 바이트), "width_mm", "height_mm"} 한 장. 적용 순서는 글→칸 높이→사진이다.
    """
    doc = HwpDoc(raw)
    heights = [(cid, check_mm(cid, mm)) for cid, mm in heights]
    if photo is not None:
        photo = prepare_photo(photo)
        for aid, _, _ in answers:
            if aid == photo["id"] or aid.startswith(photo["id"] + ".p"):
                raise UserError(f"{aid} 는 사진을 넣는 칸입니다. 같은 칸에 글과 사진을 함께 지정할 수 없습니다.")
    edits = {}  # (구역, 문단 순서) -> [방식, 글자]
    applied, unknown = [], []

    def current(s_no):
        def get(para):
            mode, text = edits.get((s_no, para.ordinal), (None, ""))
            return text if mode == "replace" else para.text + text
        return get

    by_para = {m.group(1) for aid, _, _ in answers for m in [PARA_ID_RE.fullmatch(aid)] if m}
    for aid, text, mode in answers:
        text = normalize_text(text)
        if aid in by_para:
            unknown.append(f"{aid} (같은 칸의 문단 번호 답이 있어 건너뜀)")
            continue
        if (aid not in doc.targets and aid not in doc.para_targets) or encode_text(text) is None:
            unknown.append(aid)
            continue
        if aid in doc.para_targets:
            s_no, obj = doc.para_targets[aid]
            steps = plan_para(obj, mode, text, current(s_no))
        else:
            kind, s_no, obj = doc.targets[aid]
            if kind == "p":
                steps = [(obj, "replace", text)] if obj.plain else None
            else:
                steps = plan_cell(obj, mode, text, current(s_no))
        if steps is None:
            unknown.append(aid)
            continue
        for para, how, value in steps:
            key = (s_no, para.ordinal)
            if key in edits and how == "append":
                edits[key][1] += value
            else:
                edits[key] = [how, value]
        applied.append(aid)
    new_raw, changed, current, dirty = raw, Changed(), {}, set()
    for s_no, section in enumerate(doc.sections):
        recs = section["recs"]
        paras = scan_paragraphs(recs)
        by_ordinal = {p.ordinal: p for p in paras.values()}
        todo = sorted(((o, e) for (s, o), e in edits.items() if s == s_no), reverse=True)
        touched, expected = set(), {}
        new_recs = list(recs)
        for ordinal, (how, value) in todo:
            para = by_ordinal[ordinal]
            replacement = rewrite_para(recs, para, how, value)
            expected[ordinal] = value if how == "replace" else para.text + value
            if replacement != recs[para.i:para.end]:
                touched.add(ordinal)
            new_recs[para.i:para.end] = replacement
        if not todo:
            continue
        current[s_no] = new_recs
        if touched:
            dirty.add(s_no)
        changed[section["name"]] = {"touched": touched, "expected": expected}
    sections = [current.get(n, list(sec["recs"])) for n, sec in enumerate(doc.sections)]
    if heights:
        cells = change_heights(sections, heights)
        dirty |= {c["section"] for c in cells if c["old"] != c["new"]}
        changed.heights = {"cells": [dict(c, section=doc.sections[c["section"]]["name"]) for c in cells]}
    docinfo = None
    if photo is not None:
        docinfo = parse_records(inflate(doc.cfb.read("DocInfo")) if doc.compressed else doc.cfb.read("DocInfo"))
        bin_id, bin_name = add_bin_data(docinfo, doc.cfb.streams, photo["ext"])
        s_no, first, last = insert_photo(sections, photo, bin_id)
        dirty.add(s_no)
        name = doc.sections[s_no]["name"]
        if name in changed:  # 사진 칸의 문단이 줄어든 만큼 뒤 문단 번호를 당긴다
            shift = last - first
            fix = lambda o: o - shift if o > last else o
            plan = changed[name]
            plan["touched"] = {fix(o) for o in plan["touched"]}
            plan["expected"] = {fix(o): t for o, t in plan["expected"].items()}
        changed.photo = {"id": photo["id"], "section": name, "bin": "BinData/" + bin_name, "bin_id": bin_id,
                         "data": photo["data"], "w": photo["w"], "h": photo["h"]}
    writes = {}
    for n in sorted(dirty):
        data = build_records(sections[n])
        writes[doc.sections[n]["name"]] = deflate(data) if doc.compressed else data
    if photo is not None:
        info_data = build_records(docinfo)
        stored = deflate_max(photo["data"]) if doc.compressed else photo["data"]
        reserve = sum(-(-len(e) // SS) for e in writes.values()) + -(-len(info_data) // SS) + 2
        new_raw = add_stream(new_raw, "BinData", bin_name, stored, reserve)
        new_raw = rewrite_docinfo(new_raw, info_data, doc.compressed)
    for name, data in writes.items():
        new_raw = rebuild_ole(new_raw, name, data)
    return new_raw, applied, unknown, [], changed


# ---------- 사진·칸 높이 ----------

SOF_MARKERS = set(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}
MAX_MM = 1000
MAX_PIXELS = 20000


def image_info(data):
    """JPEG·PNG 머리를 직접 읽어 (확장자, 가로 픽셀, 세로 픽셀)을 돌려준다. 다른 형식이거나 머리가 깨졌으면 UserError."""
    bad = UserError("사진 파일의 머리를 읽지 못했습니다. 깨지지 않은 JPEG 나 PNG 를 넣어 주세요.")
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        if len(data) < 33 or data[12:16] != b"IHDR" or struct.unpack_from(">I", data, 8)[0] != 13:
            raise bad
        if zlib.crc32(data[12:29]) != struct.unpack_from(">I", data, 29)[0]:
            raise bad
        ext, (w, h) = "png", struct.unpack_from(">II", data, 16)
    elif data[:2] == b"\xff\xd8":
        ext, w, h, i = "jpg", 0, 0, 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                raise bad
            m = data[i + 1]
            if m == 0xFF:
                i += 1
            elif m == 0x01 or 0xD0 <= m <= 0xD8:
                i += 2
            elif m in (0xD9, 0xDA):
                break
            else:
                seg = struct.unpack_from(">H", data, i + 2)[0]
                if m in SOF_MARKERS:
                    if seg < 8 or i + 9 > len(data):
                        raise bad
                    h, w = struct.unpack_from(">HH", data, i + 5)
                    break
                i += 2 + max(seg, 2)
    else:
        raise UserError("JPEG 와 PNG 사진만 넣을 수 있습니다.")
    if not w or not h:
        raise bad
    if w > MAX_PIXELS or h > MAX_PIXELS:
        raise UserError(f"사진이 너무 큽니다. 가로·세로 {MAX_PIXELS}픽셀 이하의 사진을 넣어 주세요.")
    return ext, w, h


def check_mm(cid, mm):
    if not isinstance(cid, str) or not PHOTO_CELL_RE.fullmatch(cid):
        raise UserError(f"{cid} 은 칸 번호(예: t0.r1.c5)가 아닙니다. 사진과 칸 높이는 칸 번호로만 지정할 수 있습니다.")
    if isinstance(mm, bool) or not isinstance(mm, (int, float)) or not 0 < mm <= MAX_MM:
        raise UserError(f"{cid} 의 mm 값은 0 보다 크고 {MAX_MM} 이하인 숫자여야 합니다.")
    return mm


def mm_units(mm):
    return round(mm / 25.4 * 7200)


def prepare_photo(photo):
    check_mm(photo["id"], photo["width_mm"])
    check_mm(photo["id"], photo["height_mm"])
    ext, px_w, px_h = image_info(photo["data"])
    return {"id": photo["id"], "data": photo["data"], "ext": ext, "px": (px_w, px_h),
            "w": mm_units(photo["width_mm"]), "h": mm_units(photo["height_mm"])}


def index_cells(sections):
    """구역별 레코드 목록 -> {칸 번호: (구역 번호, 표, 칸)}. 표 번호는 HwpDoc 과 같은 방식으로 센다."""
    out, t_no = {}, 0
    for s_no, recs in enumerate(sections):
        _, tables = index_section(recs, t_no)
        t_no += len(tables)
        for t in tables:
            for (row, col), cell in t["cells"].items():
                out[f"t{t['no']}.r{row}.c{col}"] = (s_no, t, cell)
    return out


def row_heights(table, recs):
    """행 번호 -> 그 행에서 시작하는 세로 한 칸짜리 칸들의 가장 큰 높이."""
    out = {}
    for (row, _), cell in table["cells"].items():
        h = struct.unpack_from("<I", recs[cell["head"]][2], HEIGHT_AT)[0] if len(recs[cell["head"]][2]) >= HEIGHT_AT + 4 else 0
        out[row] = max(out.get(row, 0), h) if cell["rowspan"] <= 1 else out.get(row, 0)
    return out


def change_heights(sections, heights):
    """칸 번호가 가리키는 행의 모든 칸 높이를 같은 값으로 맞춘다(LIST_HEADER 의 높이 값만 바꿈). sections 를 제자리에서 고친다.

    표마다 바꾼 행들의 높이 합계가 그대로여야 한다. 돌려주는 값: [{"id", "section", "old", "new"}] (바뀐 행에서 시작하는 모든 칸).
    """
    cells = index_cells(sections)
    want = {}
    for cid, mm in heights:
        if cid not in cells:
            raise UserError(f"{cid} 칸이 없어 높이를 바꾸지 못합니다.")
        s_no, table, cell = cells[cid]
        new = mm_units(mm)
        if want.setdefault((s_no, table["no"], cell["row"]), new) != new:
            raise UserError(f"{cid} 와 같은 행에 다른 높이가 지정되어 있습니다.")
    by_table = {}
    for (s_no, tno, row), new in want.items():
        by_table.setdefault((s_no, tno), {})[row] = new
    out = []
    for (s_no, tno), rows in sorted(by_table.items()):
        recs = sections[s_no]
        table = next(t for _, t, _ in cells.values() if t["no"] == tno)
        for cell in table["cells"].values():
            if cell["rowspan"] > 1 and any(cell["row"] <= r < cell["row"] + cell["rowspan"] for r in rows):
                raise UserError(f"표 {tno} 의 {cell['row']}행에서 시작하는 세로로 합쳐진 칸이 바꿀 행에 걸쳐 있어 높이를 바꾸지 못합니다.")
        for cell in table["cells"].values():
            if cell["row"] in rows and len(recs[cell["head"]][2]) < HEIGHT_AT + 4:
                raise UserError("표 칸 정보를 읽지 못했습니다. 파일이 깨졌을 수 있습니다.")
        old_rows = row_heights(table, recs)
        old_sum, new_sum = sum(old_rows[r] for r in rows), sum(rows.values())
        if old_sum != new_sum:
            diff = new_sum - old_sum
            raise UserError(f"표 {tno} 의 바꿀 행 높이 합계가 {old_sum} 에서 {new_sum} 로 {diff:+d}(HWPUNIT, 약 {diff / 7200 * 25.4:+.2f}mm) "
                            "달라집니다. 합계가 그대로이도록 높이를 다시 정해 주세요.")
        for key in table["order"]:
            cell = table["cells"][key]
            if cell["row"] not in rows:
                continue
            kind, lvl, payload = recs[cell["head"]]
            old = struct.unpack_from("<I", payload, HEIGHT_AT)[0]
            new = rows[cell["row"]]
            recs[cell["head"]] = [kind, lvl, payload[:HEIGHT_AT] + struct.pack("<I", new) + payload[HEIGHT_AT + 4:]]
            out.append({"id": f"t{tno}.r{cell['row']}.c{cell['col']}", "section": s_no, "old": old, "new": new})
    return out


def deflate_max(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def add_bin_data(recs, streams, ext):
    """DocInfo 레코드 목록에 BIN_DATA 를 하나 더하고 ID_MAPPINGS 의 그림 개수를 올린다. (그림 번호, 스트림 이름)을 돌려준다."""
    bins = [n for n in streams if n.startswith("BinData/")]
    idm = next((i for i, r in enumerate(recs) if r[0] == ID_MAPPINGS), None)
    last = max((i for i, r in enumerate(recs) if r[0] == BIN_DATA), default=None)
    if idm is None or last is None or len(recs[idm][2]) < 4:
        raise UserError("그림이 하나도 없는 양식이라 같은 형식을 본뜰 수 없어 사진을 넣지 못합니다.")
    count = struct.unpack_from("<I", recs[idm][2], 0)[0]
    if count != len(bins) or count != sum(1 for r in recs if r[0] == BIN_DATA):
        raise UserError("DocInfo 의 그림 개수와 BinData 스트림 수가 달라 사진을 넣지 못합니다.")
    bin_id = count + 1
    name = f"BIN{bin_id:04X}.{ext}"
    used = {struct.unpack_from("<H", r[2], 2)[0] for r in recs if r[0] == BIN_DATA and len(r[2]) >= 4}
    if bin_id in used or any(n.upper() == ("BinData/" + name).upper() for n in bins):
        raise UserError("새 그림 번호가 이미 쓰이고 있어 사진을 넣지 못합니다.")
    payload = bytearray(recs[idm][2])
    struct.pack_into("<I", payload, 0, count + 1)
    recs[idm] = [recs[idm][0], recs[idm][1], bytes(payload)]
    recs.insert(last + 1, [BIN_DATA, recs[last][1], struct.pack("<HHH", 1, bin_id, len(ext)) + ext.encode("utf-16le")])
    return bin_id, name


def picture_records(lvl, bin_id, w, ht, init_w, init_h, z, inst):
    """글자처럼 취급하는 그림 개체의 레코드(CTRL_HEADER ' osg', SHAPE_COMPONENT, SHAPE_COMPONENT_PICTURE)."""
    attr = 1 | (2 << 3) | (3 << 8) | (4 << 15) | (2 << 18) | (1 << 21) | (1 << 26)  # 글자처럼 취급, 절대 크기, 그림 번호
    ctrl = struct.pack("<4sIiiIIiHHHHIi", b" osg", attr, 0, 0, w, ht, z, 0, 0, 0, 0, inst, 0)
    comp = struct.pack("<4s4siiHHIIIIIhiiH", b"cip$", b"cip$", 0, 0, 0, 1, init_w, init_h, w, ht, 0x24000000, 0, w // 2, ht // 2, 1)
    comp += struct.pack("<6d", 1, 0, 0, 0, 1, 0) + struct.pack("<6d", w / init_w, 0, 0, 0, ht / init_h, 0) + struct.pack("<6d", 1, 0, 0, 0, 1, 0)
    pic = struct.pack("<IiI8i4i4HbbBH", 0, 0, 0, 0, 0, init_w, 0, init_w, init_h, 0, init_h, 0, 0, init_w, init_h, 0, 0, 0, 0, 0, 0, 0, bin_id)
    return [[CTRL_HEADER, lvl, ctrl], [SHAPE_COMPONENT, lvl + 1, comp], [SHAPE_PICTURE, lvl + 2, pic]]


def insert_photo(sections, photo, bin_id):
    """사진 칸의 문단을 그림 하나가 든 문단 하나로 바꾼다(sections 를 제자리에서 고침). (구역 번호, 첫 문단 번호, 끝 문단 번호)를 돌려준다."""
    cells = index_cells(sections)
    if photo["id"] not in cells:
        raise UserError(f"{photo['id']} 칸이 없어 사진을 넣지 못합니다.")
    s_no, _, cell = cells[photo["id"]]
    recs, paras = sections[s_no], cell["paras"]
    if cell["nested"] or not paras or not all(p.plain for p in paras):
        raise UserError(f"{photo['id']} 칸에는 글자 말고 다른 개체(표·그림 등)가 있어 사진으로 바꾸지 못합니다.")
    head, first = cell["head"], paras[0]
    lvl = recs[first.i][1]
    shape = next(c[2] for c in recs[first.i + 1:first.end] if c[0] == PARA_CHAR_SHAPE)
    para_head = bytearray(recs[first.i][2])
    _, _, ps, st, div, _, _, _, para_inst = struct.unpack_from("<IIHBBHHHI", para_head, 0)
    struct.pack_into("<IIHBBHHHI", para_head, 0, 0x80000000 | 9, 1 << 11, ps, st, div, 1, 0, 0, para_inst)
    text = struct.pack("<H4s8sH", 11, b" osg", b"\0" * 8, 11) + struct.pack("<H", 13)
    px_w, px_h = photo["px"]
    inst = 0x10000000 + int.from_bytes(hashlib.sha256(photo["data"] + photo["id"].encode()).digest()[:4], "big") % 0x6FFFFFFF
    new = [[PARA_HEADER, lvl, bytes(para_head)], [PARA_TEXT, lvl + 1, text], [PARA_CHAR_SHAPE, lvl + 1, shape[:8]]]
    new += picture_records(lvl + 1, bin_id, photo["w"], photo["h"], round(px_w / 96 * 7200), round(px_h / 96 * 7200), 5, inst)
    list_head = bytearray(recs[head][2])
    struct.pack_into("<H", list_head, 0, 1)
    recs[head:paras[-1].end] = [[LIST_HEADER, recs[head][1], bytes(list_head)]] + new
    return s_no, first.ordinal, paras[-1].ordinal


def rewrite_docinfo(raw, data, compressed):
    """DocInfo 를 새 내용으로 바꾼다. 작은 스트림이면 원래 미니 섹터 안에 덮어쓰고, 안 들어가면 일반 스트림으로 옮긴다."""
    cfb = Cfb(raw)
    enc = deflate_max(data) if compressed else data
    try:
        if cfb.streams["DocInfo"]["size"] < cfb.mini_cutoff and len(enc) < cfb.mini_cutoff:
            return overwrite_mini(raw, "DocInfo", enc)
    except UserError:
        pass
    return rebuild_ole(raw, "DocInfo", deflate(data) if compressed else data)


# ---------- 자체 검수 ----------

def text_groups(recs):
    """PARA_HEADER 와 그 바로 뒤의 글자 레코드(PARA_TEXT·CHAR_SHAPE·LINE_SEG)를 문단 단위로 묶는다."""
    out, cur = [], None
    for r in recs:
        if r[0] == PARA_HEADER:
            cur = [r]
            out.append(cur)
        elif r[0] in (PARA_TEXT, PARA_CHAR_SHAPE, PARA_LINE_SEG) and cur is not None:
            cur.append(r)
        else:
            cur = None
    return out


def sector_problems(cfb):
    """섹터를 두 곳이 함께 쓰거나 파일 밖을 가리키는 곳을 찾는다. 문제 설명 목록(없으면 빈 목록)."""
    total = (len(cfb.data) - SS) // SS
    owners, bad = {}, []

    def claim(label, sectors):
        for s in sectors:
            if s >= total:
                bad.append(f"{label} 가 파일 밖의 섹터 {s} 를 씀")
            elif owners.setdefault(s, label) != label:
                bad.append(f"섹터 {s} 를 {owners[s]} 와 {label} 가 함께 씀")

    claim("FAT", cfb.difat)
    claim("디렉터리", cfb.chain(cfb.dir_start, cfb.fat))
    claim("미니 FAT", cfb.chain(cfb.mini_start, cfb.fat))
    claim("미니 스트림", cfb.chain(cfb.entries[0]["start"], cfb.fat))
    mini = {}
    for path, e in cfb.streams.items():
        if e["size"] >= cfb.mini_cutoff:
            claim(path, cfb.chain(e["start"], cfb.fat))
            continue
        for ms in cfb.chain(e["start"], cfb.mini_fat):
            if (ms + 1) * MSS > len(cfb.mini_stream):
                bad.append(f"{path} 가 미니 스트림 밖의 미니 섹터 {ms} 를 씀")
            elif mini.setdefault(ms, path) != path:
                bad.append(f"미니 섹터 {ms} 를 {mini[ms]} 와 {path} 가 함께 씀")
    return bad


def docinfo_ok(old_recs, new_recs, photo):
    """DocInfo 가 'BIN_DATA 레코드 하나 추가 + ID_MAPPINGS 그림 개수 +1' 외에는 같은지."""
    j = next((i for i, r in enumerate(new_recs) if r[0] == BIN_DATA and len(r[2]) >= 4
              and struct.unpack_from("<H", r[2], 2)[0] == photo["bin_id"]), None)
    ext = photo["bin"].rsplit(".", 1)[1]
    if j is None or len(new_recs) != len(old_recs) + 1:
        return False
    if new_recs[j][2] != struct.pack("<HHH", 1, photo["bin_id"], len(ext)) + ext.encode("utf-16le"):
        return False
    counted = False
    for a, b in zip(old_recs, new_recs[:j] + new_recs[j + 1:]):
        if a[0] == ID_MAPPINGS and not counted:  # 첫 ID_MAPPINGS 는 그림 개수만 1 늘어야 한다
            counted = True
            if not (b[0] == ID_MAPPINGS and a[1] == b[1] and len(a[2]) == len(b[2]) >= 4 and a[2][4:] == b[2][4:]
                    and struct.unpack_from("<I", b[2], 0)[0] == struct.unpack_from("<I", a[2], 0)[0] + 1):
                return False
        elif a != b:
            return False
    return counted


def photo_cell_ok(recs, cell, photo):
    """사진 칸이 '문단 하나 + 그림 개체(글자처럼 취급)'이고 그림 번호·크기가 계획과 같은지."""
    if len(cell["paras"]) != 1:
        return False
    span = recs[cell["head"] + 1:cell["paras"][0].end]
    if [r[0] for r in span] != [PARA_HEADER, PARA_TEXT, PARA_CHAR_SHAPE, CTRL_HEADER, SHAPE_COMPONENT, SHAPE_PICTURE]:
        return False
    ctrl, pic = span[3][2], span[5][2]
    return (ctrl[:4] == b" osg" and len(ctrl) >= 24 and struct.unpack_from("<II", ctrl, 16) == (photo["w"], photo["h"])
            and len(pic) >= 73 and struct.unpack_from("<H", pic, 71)[0] == photo["bin_id"])


def verify(old_raw, new_raw, changed):
    """결과 파일을 다시 읽어 원본과 비교한다. 어긋난 항목 이름 목록을 돌려준다(통과하면 빈 목록)."""
    photo, heights = getattr(changed, "photo", None), getattr(changed, "heights", None)
    try:
        old, new = Cfb(old_raw), Cfb(new_raw)
        compressed, new_list = load_sections(new)
        _, old_list = load_sections(old)
    except UserError:
        return ["0. 결과 파일을 다시 읽을 수 없음"]
    bad = []
    if sorted(list(old.streams) + ([photo["bin"]] if photo else [])) != sorted(new.streams):
        return ["1. 스트림 목록이 원본과 다름"]
    h_sections = {c["section"] for c in heights["cells"]} if heights else set()
    rewritten = {n for n, plan in changed.items() if plan["touched"]} | h_sections
    if photo:
        rewritten |= {photo["section"], "DocInfo"}
    def same(n):
        try:
            return old.read(n) == new.read(n)
        except UserError:
            return False
    other = [n for n in sorted(old.streams) if n not in rewritten and not same(n)]
    if other:
        bad.append("2. 대상이 아닌 스트림의 바이트가 바뀜: " + ", ".join(other))
    old_secs, new_secs = dict(old_list), dict(new_list)
    old_cells = index_cells([r for _, r in old_list])
    new_cells = index_cells([r for _, r in new_list])
    strip = lambda recs: [[t, l, p[:HEIGHT_AT] + b"\0\0\0\0" + p[HEIGHT_AT + 4:] if t == LIST_HEADER and len(p) >= HEIGHT_AT + 4 else p]
                          for t, l, p in recs if t not in TEXT_KINDS]
    for name in list(changed) + sorted(n for n in h_sections | ({photo["section"]} if photo else set()) if n not in changed):
        plan = changed.get(name, {"touched": set(), "expected": {}})
        old_recs, new_recs = old_secs[name], new_secs[name]
        old_n, old_plain = old_recs, old_recs
        if heights:
            old_n = list(old_recs)
            for c in heights["cells"]:
                if c["section"] == name:
                    k = old_cells[c["id"]][2]["head"]
                    t, l, p = old_n[k]
                    old_n[k] = [t, l, p[:HEIGHT_AT] + struct.pack("<I", c["new"]) + p[HEIGHT_AT + 4:]]
        if photo and photo["section"] == name:
            o_cell, n_cell = old_cells[photo["id"]][2], new_cells.get(photo["id"], (0, 0, None))[2]
            if n_cell is None or not photo_cell_ok(new_recs, n_cell, photo):
                bad.append(f"10. {name} 의 사진 칸이 '문단 하나 + 그림 개체'가 아니거나 그림 번호·크기가 다름")
            else:
                if old_n[o_cell["head"]][2][2:] != new_recs[n_cell["head"]][2][2:]:
                    bad.append(f"10. {name} 의 사진 칸 머리(LIST_HEADER)가 문단 수 외에도 바뀜")
                swap = lambda recs: recs[:o_cell["head"]] + new_recs[n_cell["head"]:n_cell["paras"][0].end] + recs[o_cell["paras"][-1].end:]
                old_n, old_plain = swap(old_n), swap(old_plain)
        keep_old = [r for r in old_n if r[0] not in TEXT_KINDS]
        keep_new = [r for r in new_recs if r[0] not in TEXT_KINDS]
        if keep_old != keep_new:
            bad.append(f"3. {name} 의 글자 외 레코드(표·칸·개체 등)가 바뀜")
            if name in h_sections:
                bad.append(f"9. {name} 의 " + ("칸 높이가 계획과 다름" if strip(old_plain) == strip(new_recs) else "LIST_HEADER 높이 외 바이트가 바뀜"))
        g_old, g_new = text_groups(old_n), text_groups(new_recs)
        diff = [i for i, (a, b) in enumerate(zip(g_old, g_new)) if a != b]
        if len(g_old) != len(g_new) or diff != sorted(plan["touched"]):
            bad.append(f"4. {name} 의 문단 수가 달라졌거나 바뀐 문단이 대상과 다름(바뀐 문단 {len(diff)}개, 대상 {len(plan['touched'])}개)")
        got = {p.ordinal: p for p in scan_paragraphs(new_recs).values()}
        wrong = [o for o, text in plan["expected"].items()
                 if o not in got or got[o].text != text or got[o].nchars != max(len(got[o].payload) // 2, 1)]
        if wrong:
            bad.append(f"5. {name} 의 대상 문단 {len(wrong)}개의 글자가 답과 다름")
    if heights:
        for tno in sorted({t["no"] for c in heights["cells"] for _, t, _ in [old_cells[c["id"]]]}):
            sums = []
            for cells, lists in ((old_cells, old_list), (new_cells, new_list)):
                s_no, table, _ = next(v for v in cells.values() if v[1]["no"] == tno)
                sums.append(sum(row_heights(table, lists[s_no][1]).values()))
            if sums[0] != sums[1]:
                bad.append(f"9. 표 {tno} 의 행 높이 합계가 {sums[0]} 에서 {sums[1]} 로 바뀜")
    if photo:
        try:
            stored = new.read(photo["bin"])
            if (inflate(stored) if compressed else stored) != photo["data"]:
                bad.append("6. 새 BinData 를 풀면 사진 바이트와 다름")
        except UserError:
            bad.append("6. 새 BinData 를 풀지 못함")
    try:
        problems = sector_problems(new)
    except UserError:
        problems = ["섹터 사슬을 따라가지 못함"]
    if problems:
        bad.append("7. OLE 섹터가 어긋남: " + " / ".join(problems[:5]))
    unreadable = []
    for n in sorted(new.streams):
        try:
            new.read(n)
        except UserError:
            unreadable.append(n)
    if unreadable:
        bad.append("8. 다시 읽을 수 없는 스트림: " + ", ".join(unreadable))
    if photo:
        try:
            read_info = lambda cfb: parse_records(inflate(cfb.read("DocInfo")) if compressed else cfb.read("DocInfo"))
            if not docinfo_ok(read_info(old), read_info(new), photo):
                bad.append("10. DocInfo 가 'BIN_DATA 하나 추가 + 그림 개수 +1' 외에도 바뀜")
        except UserError:
            bad.append("10. DocInfo 를 읽지 못함")
    return bad


# ---------- 근사 미리보기 ----------

APPROX_NOTE = "근사 미리보기: 실제 한글 화면과 줄 바꿈 위치가 다를 수 있습니다"
IMAGE_MIME = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "gif": "image/gif", "bmp": "image/bmp"}
ALIGN = ["justify", "left", "right", "center", "justify", "center"]
VALIGN = ["flex-start", "center", "flex-end"]
mm_of = lambda units: units / 7200 * 25.4


class Preview:
    """.hwp 한 파일을 HTML 로 그린다. 표(칸 높이 반영)와 문단(줄 간격은 문단 모양에서), 그림(base64)을 그린다."""

    def __init__(self, raw):
        self.cfb = Cfb(raw)
        self.compressed, self.sections = load_sections(self.cfb)
        info = self.cfb.read("DocInfo")
        info = parse_records(inflate(info) if self.compressed else info)
        self.sizes = [struct.unpack_from("<i", p, 42)[0] / 100 if len(p) >= 46 else 10 for t, _, p in info if t == DOC_CHAR_SHAPE]
        self.bold = [bool(struct.unpack_from("<I", p, 46)[0] & 2) if len(p) >= 50 else False for t, _, p in info if t == DOC_CHAR_SHAPE]
        paras = [p for t, _, p in info if t == DOC_PARA_SHAPE]
        self.align = [ALIGN[min((struct.unpack_from("<I", p, 0)[0] >> 2) & 7, 5)] if len(p) >= 4 else "justify" for p in paras]
        # 줄 간격: 퍼센트 방식만 읽고 나머지 방식은 1.6
        self.spacing = [struct.unpack_from("<i", p, 24)[0] / 100 if len(p) >= 28 and struct.unpack_from("<I", p, 0)[0] & 3 == 0 else 1.6
                        for p in paras]
        self.bins = {}  # 그림 번호 -> 스트림 이름
        for t, _, p in info:
            if t == BIN_DATA and len(p) >= 6 and struct.unpack_from("<H", p, 0)[0] & 0xF == 1:
                n = struct.unpack_from("<H", p, 4)[0]
                ext = p[6:6 + n * 2].decode("utf-16le", "replace")
                self.bins[struct.unpack_from("<H", p, 2)[0]] = (f"BinData/BIN{struct.unpack_from('<H', p, 2)[0]:04X}.{ext}", ext,
                                                                struct.unpack_from("<H", p, 0)[0])

    def image(self, bin_id):
        entry = self.bins.get(bin_id)
        names = {n.upper(): n for n in self.cfb.streams}
        if entry is None or entry[1].lower() not in IMAGE_MIME or entry[0].upper() not in names:
            return None
        data = self.cfb.read(names[entry[0].upper()])
        mode = (entry[2] >> 4) & 3  # 0 문서 설정을 따름, 1 압축, 2 압축 안 함
        if mode == 1 or (mode == 0 and self.compressed):
            data = inflate(data)
        return f"data:{IMAGE_MIME[entry[1].lower()]};base64," + base64.b64encode(data).decode()

    def para(self, recs, i):
        """레코드 i 의 문단을 그린 (HTML, 끝 번호). 문단 아래의 그림과 표도 함께 그린다."""
        lvl, payload = recs[i][1], recs[i][2]
        ps = struct.unpack_from("<H", payload, 8)[0]
        out, text, cs, j, pics, tables = [], [], 0, i + 1, [], []
        while j < len(recs) and recs[j][1] > lvl:
            tag, l, p = recs[j]
            if l == lvl + 1 and tag == PARA_TEXT:
                units = struct.unpack("<%dH" % (len(p) // 2), p[:len(p) // 2 * 2])
                k = 0
                while k < len(units):
                    u = units[k]
                    if u in WIDE_CONTROLS:
                        if u == 9:
                            text.append("&emsp;")
                        k += 8
                        continue
                    k += 1
                    if u == 10:
                        text.append("<br>")
                    elif u == 32:
                        text.append("&nbsp;")
                    elif u >= 32:
                        text.append(html.escape(chr(u)))
            elif l == lvl + 1 and tag == PARA_CHAR_SHAPE and len(p) >= 8:
                cs = struct.unpack_from("<I", p, 4)[0]
            elif l == lvl + 1 and tag == CTRL_HEADER and p[:4] == b" osg" and len(p) >= 24:
                w, h = struct.unpack_from("<II", p, 16)
                k = j + 1
                while k < len(recs) and recs[k][1] > l:
                    if recs[k][0] == SHAPE_PICTURE and len(recs[k][2]) >= 73:
                        src = self.image(struct.unpack_from("<H", recs[k][2], 71)[0])
                        if src:
                            pics.append(f'<img src="{src}" style="width:{mm_of(w):.2f}mm;height:{mm_of(h):.2f}mm">')
                    k += 1
            elif l == lvl + 1 and tag == CTRL_HEADER and p[:4] == TABLE_CTRL:
                tables.append(self.table(recs, j))
            j += 1
        size = self.sizes[cs] if cs < len(self.sizes) else 10
        bold = "font-weight:700;" if cs < len(self.bold) and self.bold[cs] else ""
        align = self.align[ps] if ps < len(self.align) else "justify"
        spacing = self.spacing[ps] if ps < len(self.spacing) else 1.6
        body = "".join(text).encode("utf-16", "surrogatepass").decode("utf-16", "replace")
        out.append(f'<div class="p" style="text-align:{align};font-size:{size}pt;line-height:{spacing};{bold}">'
                   f'{"".join(pics)}{body or ("" if pics else "&nbsp;")}</div>')
        return "".join(out) + "".join(tables), j

    def table(self, recs, i):
        lvl, j, cells = recs[i][1], i + 1, []
        while j < len(recs) and recs[j][1] > lvl:
            tag, l, p = recs[j]
            if tag == LIST_HEADER and l == lvl + 1 and len(p) >= 32:
                prop, col, row, cspan, rspan, w, h = struct.unpack_from("<4xIHHHHII", p, 0)
                margins = struct.unpack_from("<4H", p, 24)
                cell = {"col": col, "row": row, "cs": max(cspan, 1), "rs": max(rspan, 1), "w": w, "h": h, "m": margins,
                        "va": VALIGN[((prop >> 5) & 3) % 3], "html": ""}
                cells.append(cell)
                j += 1
                while j < len(recs) and recs[j][1] >= l and not (recs[j][0] == LIST_HEADER and recs[j][1] == l):
                    if recs[j][0] == PARA_HEADER and recs[j][1] == l:
                        block, j = self.para(recs, j)
                        cell["html"] += block
                    else:
                        j += 1
            else:
                j += 1
        if not cells:
            return ""
        edges = {0: 0}  # 열 경계 번호 -> 왼쪽에서 떨어진 거리
        for _ in range(max(c["col"] + c["cs"] for c in cells) + 2):
            for c in cells:
                if c["col"] in edges:
                    edges.setdefault(c["col"] + c["cs"], edges[c["col"]] + c["w"])
        keys = sorted(edges)
        cols = " ".join(f"{mm_of(edges[b] - edges[a]):.2f}mm" for a, b in zip(keys, keys[1:])) or "auto"
        out = [f'<div class="tbl" style="grid-template-columns:{cols}">']
        for c in cells:
            start = keys.index(c["col"]) + 1 if c["col"] in keys else c["col"] + 1
            end = keys.index(c["col"] + c["cs"]) + 1 if c["col"] + c["cs"] in keys else start + c["cs"]
            m = c["m"]
            out.append(f'<div class="c" style="grid-column:{start}/{end};grid-row:{c["row"] + 1}/span {c["rs"]};'
                       f'min-height:{mm_of(c["h"]):.2f}mm;padding:{mm_of(m[2]):.2f}mm {mm_of(m[1]):.2f}mm {mm_of(m[3]):.2f}mm {mm_of(m[0]):.2f}mm;'
                       f'justify-content:{c["va"]}">{c["html"]}</div>')
        return "".join(out) + "</div>"

    def render(self):
        body, page = [], None
        for _, recs in self.sections:
            page = page or next((p for t, _, p in recs if t == PAGE_DEF and len(p) >= 24), None)
            i = 0
            while i < len(recs):
                if recs[i][0] == PARA_HEADER and recs[i][1] == 0:
                    brk = ' style="break-before:page"' if len(recs[i][2]) > 11 and recs[i][2][11] & 4 and body else ""
                    block, i = self.para(recs, i)
                    body.append(f"<section{brk}>{block}</section>")
                else:
                    i += 1
        pw, ph, ml, mr, mt, mb = struct.unpack_from("<6I", page, 0) if page else (59528, 84188, 8504, 8504, 5668, 4252)
        return f"""<!doctype html><meta charset="utf-8"><title>{APPROX_NOTE}</title><style>
@page {{ size: {mm_of(pw):.1f}mm {mm_of(ph):.1f}mm; margin: {mm_of(mt):.1f}mm {mm_of(mr):.1f}mm {mm_of(mb):.1f}mm {mm_of(ml):.1f}mm; }}
body {{ font-family: Gulim, "굴림", AppleGothic, "Apple SD Gothic Neo", sans-serif; margin:0; }}
.tbl {{ display:grid; border-top:0.12mm solid #000; border-left:0.12mm solid #000; margin:2mm 0; }}
.c {{ border-right:0.12mm solid #000; border-bottom:0.12mm solid #000; display:flex; flex-direction:column; box-sizing:border-box; }}
.p {{ word-break:keep-all; overflow-wrap:anywhere; }}
.note {{ font-size:9pt; color:#a00; margin:0 0 2mm; }}
</style><p class="note">{APPROX_NOTE}</p>{"".join(body)}"""


def render_html(raw):
    """채운 .hwp 를 HTML 근사 미리보기로 바꾼다(첫머리에 근사라는 안내 문구가 있다). 읽지 못하면 UserError 나 그 밖의 오류가 난다."""
    return Preview(raw).render()
