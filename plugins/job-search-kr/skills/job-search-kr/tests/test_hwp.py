"""옛 한글(.hwp) 지원 시험. 작은 OLE(CFB) 파일과 한글 본문 레코드를 표준 라이브러리로 직접 만들어 쓴다.

    python3 -m pytest -q tests/test_hwp.py
"""
import contextlib
import hashlib
import io
import json
import os
import random
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _common  # noqa: E402
import form_tool as ft  # noqa: E402
import hwp  # noqa: E402

FREESECT, ENDOFCHAIN, FATSECT, NOSTREAM = 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFFD, 0xFFFFFFFF


# ---------- 시험용 파일 만들기 ----------

def make_cfb(streams, min_fat_sectors=3, red=()):
    """streams: {경로: 바이트}. 경로 'A/B' 는 저장소 A 안의 스트림 B. FAT 에는 여유 칸을 둔다. red 의 경로는 빨강, 나머지는 검정."""
    entries = [{"name": "Root Entry", "type": 5, "kids": [], "start": ENDOFCHAIN, "size": 0}]
    index = {"": 0}
    for path, data in streams.items():
        parts = path.split("/")
        parent = ""
        for part in parts[:-1]:
            key = f"{parent}/{part}" if parent else part
            if key not in index:
                entries.append({"name": part, "type": 1, "kids": [], "start": 0, "size": 0})
                index[key] = len(entries) - 1
                entries[index[parent]]["kids"].append(index[key])
            parent = key
        entries.append({"name": parts[-1], "type": 2, "kids": [], "start": 0, "size": len(data), "data": data,
                        "color": 0 if path in red else 1})
        entries[index[parent]]["kids"].append(len(entries) - 1)
    small = [e for e in entries if e["type"] == 2 and e["size"] < 4096]
    big = [e for e in entries if e["type"] == 2 and e["size"] >= 4096]
    mini, mini_fat = bytearray(), []
    for e in small:
        n = -(-e["size"] // 64)
        e["start"] = len(mini) // 64
        mini_fat += [e["start"] + k + 1 for k in range(n - 1)] + [ENDOFCHAIN]
        mini += e["data"] + b"\0" * (n * 64 - e["size"])
    n_dir = -(-len(entries) // 4)
    n_minifat = -(-len(mini_fat) // 128)
    n_mini = -(-len(mini) // 512)
    n_big = sum(-(-e["size"] // 512) for e in big)
    nfat = max(min_fat_sectors, 1)
    fat = [FREESECT] * (nfat * 128)
    cursor = [0]
    for _ in range(nfat):
        fat[cursor[0]] = FATSECT
        cursor[0] += 1

    def alloc(n):
        secs = list(range(cursor[0], cursor[0] + n))
        cursor[0] += n
        for a, b in zip(secs, secs[1:] + [ENDOFCHAIN]):
            fat[a] = b
        return secs

    dir_secs = alloc(n_dir)
    minifat_secs = alloc(n_minifat) if n_minifat else []
    mini_secs = alloc(n_mini) if n_mini else []
    body = {}
    for e in big:
        secs = alloc(-(-e["size"] // 512))
        e["start"] = secs[0]
        body[secs[0]] = (secs, e["data"])
    root = entries[0]
    root["start"], root["size"] = (mini_secs[0] if mini_secs else ENDOFCHAIN), len(mini)
    for e in entries:
        kids = e["kids"]
        e["child"] = kids[0] if kids else NOSTREAM
        for a, b in zip(kids, kids[1:]):
            entries[a]["right"] = b
    sectors = {}
    for s in range(nfat):
        sectors[s] = struct.pack("<128I", *fat[s * 128:(s + 1) * 128])
    raw_dir = b""
    for e in entries:
        name = e["name"].encode("utf-16le")
        raw_dir += (name + b"\0" * (64 - len(name)) + struct.pack("<HBB", len(name) + 2, e["type"], e.get("color", 1))
                    + struct.pack("<III", NOSTREAM, e.get("right", NOSTREAM), e["child"]) + b"\0" * 36
                    + struct.pack("<III", e["start"], e["size"], 0))
    raw_dir += b"\0" * (n_dir * 512 - len(raw_dir))
    for k, s in enumerate(dir_secs):
        sectors[s] = raw_dir[k * 512:(k + 1) * 512]
    padded = mini_fat + [FREESECT] * (n_minifat * 128 - len(mini_fat))
    for k, s in enumerate(minifat_secs):
        sectors[s] = struct.pack("<128I", *padded[k * 128:(k + 1) * 128])
    mini = bytes(mini) + b"\0" * (n_mini * 512 - len(mini))
    for k, s in enumerate(mini_secs):
        sectors[s] = mini[k * 512:(k + 1) * 512]
    for secs, data in body.values():
        data += b"\0" * (len(secs) * 512 - len(data))
        for k, s in enumerate(secs):
            sectors[s] = data[k * 512:(k + 1) * 512]
    difat = list(range(nfat)) + [FREESECT] * (109 - nfat)
    header = (hwp.SIGNATURE + b"\0" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6) + b"\0" * 6
              + struct.pack("<IIIIIIIII", 0, nfat, dir_secs[0], 0, 4096,
                            minifat_secs[0] if minifat_secs else ENDOFCHAIN, n_minifat, ENDOFCHAIN, 0)
              + struct.pack("<109I", *difat))
    assert len(header) == 512
    body_bytes = b"".join(sectors.get(s, b"\0" * 512) for s in range(cursor[0]))
    return header + body_bytes


def rec(tag, lvl, payload=b""):
    if len(payload) >= 0xFFF:
        return struct.pack("<II", tag | lvl << 10 | 0xFFF << 20, len(payload)) + payload
    return struct.pack("<I", tag | lvl << 10 | len(payload) << 20) + payload


def para(lvl, text="", ctrls=(), last=False, ps=0):
    """문단 레코드. ctrls 의 각 항목은 (레벨) -> 바이트 인 함수로, 문단 아래(레벨+1)에 개체를 단다."""
    units = [10 if c == "\n" else ord(c) for c in text]
    placeholder = [11, 0, 0, 0, 0, 0, 0, 11] * len(ctrls)
    if units or ctrls:
        units = placeholder + units + [13]
    count = len(units) or 1
    mask = (1 << 10 if 10 in units else 0) | (1 << 11 if ctrls else 0)
    head = struct.pack("<IIHBBHHHI", count | (0x80000000 if last else 0), mask, ps, 0, 0, 1, 0, 1, 0)
    out = rec(hwp.PARA_HEADER, lvl, head)
    if units:
        out += rec(hwp.PARA_TEXT, lvl + 1, struct.pack("<%dH" % len(units), *units))
    out += rec(hwp.PARA_CHAR_SHAPE, lvl + 1, struct.pack("<II", 0, 7))
    out += rec(hwp.PARA_LINE_SEG, lvl + 1, b"\x01" * 36)
    return out + b"".join(c(lvl + 1) for c in ctrls)


def table(lvl, cells):
    """cells: [(행, 열, 가로칸, 세로칸, [문단 글자 또는 (글자, 개체들)…])]"""
    out = rec(hwp.CTRL_HEADER, lvl, b" lbt" + b"\0" * 40) + rec(hwp.TABLE, lvl + 1, b"\0" * 34)
    for row, col, cs, rs, paras, *size in cells:  # size: [가로, 세로](HWPUNIT), 없으면 0
        out += rec(hwp.LIST_HEADER, lvl + 1, struct.pack("<HHIHHHH", len(paras), 0, 0x20, col, row, cs, rs)
                   + struct.pack("<II", *(size or (0, 0))) + b"\0" * 14)
        for k, p in enumerate(paras):
            text, ctrls = (p, ()) if isinstance(p, str) else p
            out += para(lvl + 1, text, ctrls, last=k == len(paras) - 1)
    return out


def header_ctrl(inner_table_cells):
    def build(lvl):
        return (rec(hwp.CTRL_HEADER, lvl, b"daeh" + b"\0" * 8) + rec(hwp.LIST_HEADER, lvl + 1, b"\0" * 34)
                + para(lvl + 1, "", [lambda lv: table(lv, inner_table_cells)]))
    return build


def filler(count, seed=1):
    rnd = random.Random(seed)
    return b"".join(para(0, "".join(chr(0xAC00 + rnd.randrange(2000)) for _ in range(40))) for _ in range(count))


def section_body(extra=b"", filler_count=80):
    nested = lambda lv: table(lv, [(0, 0, 1, 1, ["안쪽"])])
    return (para(0, "입사지원서")
            + para(0, "", [lambda lv: table(lv, [
                (0, 0, 1, 1, ["성명"]), (0, 1, 1, 1, [""]),
                (1, 0, 1, 1, ["지원동기(500자 이내)", ""]), (1, 1, 1, 1, ["첫째", "둘째"]),
                (2, 0, 2, 1, ["합계"])])])
            + para(0, "자기소개")
            + para(0, "머리말문단", [header_ctrl([(0, 0, 1, 1, ["회사명"])])])
            + para(0, "", [lambda lv: table(lv, [(0, 0, 1, 1, [("", [nested]), ""]), (0, 1, 1, 1, ["표 둘째칸"])])])
            + para(0, "끝")
            + extra + filler(filler_count))


def deflate_raw(data):
    c = zlib.compressobj(9, zlib.DEFLATED, -15)
    return c.compress(data) + c.flush()


def make_hwp(sections=None, compressed=False, attrs=0, extra=None, filler_count=80):
    sections = sections or [section_body(filler_count=filler_count)]
    header = b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000300, attrs | (1 if compressed else 0))
    streams = {"FileHeader": header.ljust(256, b"\0"), "DocInfo": random.Random(3).randbytes(5000),
               "PrvText": "미리보기".encode("utf-16le")}
    for n, data in enumerate(sections):
        streams[f"BodyText/Section{n}"] = deflate_raw(data) if compressed else data
    streams.update(extra or {})
    return make_cfb(streams)


# ---------- 공통 준비 ----------

class HwpBase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "forms").mkdir()
        (self.root / "work").mkdir()
        for target, value, path in [(ft, "ROOT", self.root), (ft, "WORK", self.root / "work"),
                                    (ft, "FORMS", self.root / "forms"), (_common, "APPLICATIONS", self.root / "applications")]:
            patcher = mock.patch.object(target, value, path)
            patcher.start()
            self.addCleanup(patcher.stop)

    def install(self, raw, name="지원서.hwp"):
        (self.root / "forms" / name).write_bytes(raw)
        return raw

    def run_cmd(self, name):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            try:
                code = ft.inspect() if name == "inspect" else ft.fill()
            except ft.UserError as exc:
                return 1, f"오류: {exc}"
        return code, out.getvalue()

    def fields(self):
        return json.loads((self.root / "work" / "form-fields.json").read_text(encoding="utf-8"))

    def answer(self, answers):
        data = {"folder": "회사-직무", "answers": answers}
        (self.root / "work" / "form-answers.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def output(self, name="지원서-작성.hwp"):
        return self.root / "applications" / "회사-직무" / name

    def reopen(self, name="지원서-작성.hwp"):
        return hwp.HwpDoc(self.output(name).read_bytes())

    @staticmethod
    def cell_texts(doc, cid):
        return [p.text for p in doc.targets[cid][2]["paras"]]


# ---------- 레코드·OLE ----------

class RecordTest(unittest.TestCase):
    def test_record_roundtrip(self):
        big = bytes(range(256)) * 20  # 5120바이트: 확장 길이 필드를 쓴다
        data = rec(66, 0, b"\1\2\3") + rec(67, 1, b"") + rec(68, 3, big) + rec(69, 2, b"x" * 0xFFE) + rec(70, 1, b"y" * 0xFFF)
        recs = hwp.parse_records(data)
        self.assertEqual([(r[0], r[1], len(r[2])) for r in recs],
                         [(66, 0, 3), (67, 1, 0), (68, 3, 5120), (69, 2, 0xFFE), (70, 1, 0xFFF)])
        self.assertEqual(recs[2][2], big)
        self.assertEqual(hwp.build_records(recs), data)

    def test_truncated_record_refused(self):
        with self.assertRaises(ft.UserError):
            hwp.parse_records(rec(66, 0, b"abcdef")[:-2])


class CfbTest(unittest.TestCase):
    def streams(self):
        return {"FileHeader": b"H" * 256, "BodyText/Section0": random.Random(5).randbytes(9000),
                "BodyText/Section1": random.Random(6).randbytes(5000), "Small": "작은 스트림".encode("utf-16le"),
                "Other/Deep": random.Random(7).randbytes(4100)}

    def test_read_streams_by_name(self):
        streams = self.streams()
        cfb = hwp.Cfb(make_cfb(streams))
        self.assertEqual(sorted(cfb.streams), sorted(streams))
        for name, data in streams.items():
            self.assertEqual(cfb.read(name), data, name)

    def test_rebuild_moves_only_the_changed_stream(self):
        streams = self.streams()
        raw = make_cfb(streams)
        new = random.Random(8).randbytes(12000)
        out = hwp.rebuild_ole(raw, "BodyText/Section0", new)
        self.assertEqual(out[:len(raw)][:44], raw[:44])
        cfb = hwp.Cfb(out)
        self.assertEqual(sorted(cfb.streams), sorted(streams))
        self.assertEqual(cfb.read("BodyText/Section0"), new)
        for name in streams:
            if name != "BodyText/Section0":
                self.assertEqual(cfb.read(name), streams[name], name)
        # 다시 한 번 다른 스트림을 옮겨도 앞서 바꾼 것이 남는다
        again = hwp.Cfb(hwp.rebuild_ole(out, "BodyText/Section1", b"z" * 6000))
        self.assertEqual(again.read("BodyText/Section0"), new)
        self.assertEqual(again.read("BodyText/Section1"), b"z" * 6000)

    def test_rebuild_from_mini_stream_to_big(self):
        streams = self.streams()
        streams["Mini"] = b"m" * 300
        out = hwp.rebuild_ole(make_cfb(streams), "Mini", b"n" * 5000)
        cfb = hwp.Cfb(out)
        self.assertEqual(cfb.read("Mini"), b"n" * 5000)
        self.assertEqual(cfb.read("Small"), streams["Small"])

    def test_rebuild_refuses_tiny_stream(self):
        with self.assertRaises(ft.UserError) as ctx:
            hwp.rebuild_ole(make_cfb(self.streams()), "BodyText/Section0", b"x" * 100)
        self.assertIn("4096", str(ctx.exception))

    def test_difat_refused(self):
        raw = bytearray(make_cfb(self.streams()))
        struct.pack_into("<I", raw, 72, 1)  # DIFAT 섹터 수
        with self.assertRaises(ft.UserError) as ctx:
            hwp.Cfb(bytes(raw))
        self.assertIn("DIFAT", str(ctx.exception))

    def test_not_ole_refused(self):
        with self.assertRaises(ft.UserError):
            hwp.Cfb(b"PK\3\4" + b"\0" * 600)


# ---------- inspect ----------

class InspectTest(HwpBase):
    def test_inspect_lists_paragraphs_and_cells(self):
        self.install(make_hwp())
        code, out = self.run_cmd("inspect")
        self.assertEqual(code, 0, out)
        self.assertIn("표 4개(칸 9개)", out)
        data = self.fields()
        self.assertEqual(data["type"], "hwp")
        ps = {p["id"]: p for p in data["paragraphs"]}
        # 표만 품은 빈 문단은 번호를 받지 않는다
        self.assertEqual([ps[f"p{i}"]["text"] for i in range(4)], ["입사지원서", "자기소개", "머리말문단", "끝"])
        self.assertEqual(ps["p1"]["before"], "")  # 표 바로 뒤라서 앞 글자를 비운다
        self.assertEqual(ps["p2"]["before"], "자기소개")
        self.assertFalse(ps["p0"]["empty"])
        self.assertEqual([t["id"] for t in data["tables"]], ["t0", "t1", "t2", "t3"])

    def test_inspect_cell_fields(self):
        self.install(make_hwp())
        self.run_cmd("inspect")
        t0 = self.fields()["tables"][0]
        self.assertEqual([[c["id"] for c in row] for row in t0["rows"]],
                         [["t0.r0.c0", "t0.r0.c1"], ["t0.r1.c0", "t0.r1.c1"], ["t0.r2.c0"]])
        cells = {c["id"]: c for row in t0["rows"] for c in row}
        self.assertEqual(cells["t0.r0.c0"], {"id": "t0.r0.c0", "text": "성명", "addr": [0, 0]})
        self.assertEqual(cells["t0.r1.c0"]["text"], "지원동기(500자 이내)")
        self.assertEqual(cells["t0.r1.c1"]["text"], "첫째\n둘째")
        self.assertEqual(cells["t0.r2.c0"]["span"], 2)
        self.assertNotIn("rowspan", cells["t0.r2.c0"])

    def test_header_table_and_nested_table_are_numbered_in_document_order(self):
        self.install(make_hwp())
        self.run_cmd("inspect")
        tables = {t["id"]: t for t in self.fields()["tables"]}
        self.assertEqual(tables["t1"]["rows"][0][0]["text"], "회사명")  # 머리말 안의 표
        outer = {c["id"]: c for row in tables["t2"]["rows"] for c in row}
        self.assertTrue(outer["t2.r0.c0"]["nested_table"])
        self.assertNotIn("nested_table", outer["t2.r0.c1"])
        self.assertEqual(outer["t2.r0.c1"]["text"], "표 둘째칸")  # 안쪽 표 뒤의 칸도 읽는다
        self.assertEqual(tables["t3"]["rows"][0][0]["text"], "안쪽")

    def test_multi_paragraph_cell_lists_each_paragraph(self):
        self.install(make_hwp())
        self.run_cmd("inspect")
        cells = {c["id"]: c for t in self.fields()["tables"] for row in t["rows"] for c in row}
        self.assertEqual(cells["t0.r1.c1"]["cell_paragraphs"], [
            {"id": "t0.r1.c1.p0", "text": "첫째", "empty": False},
            {"id": "t0.r1.c1.p1", "text": "둘째", "empty": False}])
        self.assertEqual(cells["t0.r1.c0"]["cell_paragraphs"], [
            {"id": "t0.r1.c0.p0", "text": "지원동기(500자 이내)", "empty": False},
            {"id": "t0.r1.c0.p1", "text": "", "empty": True}])
        self.assertEqual(cells["t0.r1.c1"]["id"], "t0.r1.c1")  # 칸 항목은 그대로 있다

    def test_single_paragraph_cell_has_no_paragraph_entries(self):
        self.install(make_hwp())
        self.run_cmd("inspect")
        cells = {c["id"]: c for t in self.fields()["tables"] for row in t["rows"] for c in row}
        self.assertNotIn("cell_paragraphs", cells["t0.r0.c0"])
        self.assertNotIn("cell_paragraphs", cells["t2.r0.c1"])

    def test_paragraph_with_object_is_marked_unfillable(self):
        self.install(make_hwp())
        self.run_cmd("inspect")
        cells = {c["id"]: c for t in self.fields()["tables"] for row in t["rows"] for c in row}
        first, second = cells["t2.r0.c0"]["cell_paragraphs"]
        self.assertEqual(first["id"], "t2.r0.c0.p0")
        self.assertIs(first["fillable"], False)
        self.assertIn("개체", first["skip"])
        self.assertNotIn("fillable", second)
        self.assertNotIn("skip", second)

    def test_cell_and_paragraph_counts_in_summary_do_not_change(self):
        self.install(make_hwp())
        code, out = self.run_cmd("inspect")
        self.assertEqual(code, 0, out)
        self.assertIn("표 4개(칸 9개)", out)

    def test_nested_table_notice_for_hwp_uses_document_order(self):
        self.install(make_hwp())
        code, out = self.run_cmd("inspect")
        self.assertIn("주의: 표 안에 표가 있는 칸이 있습니다. 안쪽 표도 문서에 나온 순서대로 표 번호를 받습니다.", out)
        self.assertNotIn("칸 번호가 따로 매겨지지 않습니다", out)

    def test_nested_table_notice_for_docx_is_unchanged(self):
        nested = {"tables": [{"id": "t0", "rows": [[{"id": "t0.r0.c0", "text": "", "nested_table": True}]]}]}
        with mock.patch.object(ft, "find_form", return_value=Path("양식.docx")), \
                mock.patch.object(ft, "open_package", return_value=contextlib.nullcontext()), \
                mock.patch.object(ft, "read_docx", return_value=([], nested["tables"], [], [])):
            code, out = self.run_cmd("inspect")
        self.assertEqual(code, 0, out)
        self.assertIn("주의: 표 안에 표가 있는 칸이 있습니다. 안쪽 표는 칸 번호가 따로 매겨지지 않습니다.", out)
        self.assertNotIn("문서에 나온 순서대로", out)

    def test_tables_are_numbered_across_sections(self):
        second = para(0, "", [lambda lv: table(lv, [(0, 0, 1, 1, ["둘째 구역 칸"])])]) + para(0, "둘째 구역 글")
        self.install(make_hwp([section_body(), second + filler(80, seed=2)]))
        self.run_cmd("inspect")
        data = self.fields()
        self.assertEqual([t["id"] for t in data["tables"]], ["t0", "t1", "t2", "t3", "t4"])
        self.assertEqual(data["tables"][4]["rows"][0][0]["text"], "둘째 구역 칸")
        self.assertIn("둘째 구역 글", [p["text"] for p in data["paragraphs"]])

    def test_compressed_document_is_read(self):
        self.install(make_hwp(compressed=True))
        code, out = self.run_cmd("inspect")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.fields()["tables"][0]["rows"][0][0]["text"], "성명")

    def test_encrypted_document_refused(self):
        self.install(make_hwp(attrs=2))
        code, out = self.run_cmd("inspect")
        self.assertEqual(code, 1)
        self.assertIn("암호", out)

    def test_distributed_document_refused(self):
        self.install(make_hwp(attrs=4))
        code, out = self.run_cmd("inspect")
        self.assertEqual(code, 1)
        self.assertIn("배포용", out)

    def test_difat_file_refused_by_command(self):
        raw = bytearray(make_hwp())
        struct.pack_into("<I", raw, 72, 1)
        self.install(bytes(raw))
        code, out = self.run_cmd("inspect")
        self.assertEqual(code, 1)
        self.assertIn("DIFAT", out)


# ---------- fill ----------

class FillTest(HwpBase):
    compressed = False

    def prepare(self, answers, **kw):
        raw = self.install(make_hwp(compressed=self.compressed, **kw))
        self.answer(answers)
        return raw

    def fill_ok(self, answers, **kw):
        raw = self.prepare(answers, **kw)
        code, out = self.run_cmd("fill")
        self.assertEqual(code, 0, out)
        return raw, out

    def test_replace_writes_first_paragraph_and_clears_the_rest(self):
        raw, out = self.fill_ok([{"id": "t0.r1.c1", "text": "가\n나"}, {"id": "t0.r0.c1", "text": "홍길동"}])
        self.assertIn("만들었습니다: applications/회사-직무/지원서-작성.hwp", out)
        self.assertIn("채운 칸 2개", out)
        doc = self.reopen()
        self.assertEqual(self.cell_texts(doc, "t0.r1.c1"), ["가\n나", ""])
        self.assertEqual(self.cell_texts(doc, "t0.r0.c1"), ["홍길동"])
        self.assertEqual(self.cell_texts(doc, "t0.r0.c0"), ["성명"])

    def test_paragraph_count_and_non_text_records_unchanged(self):
        raw, _ = self.fill_ok([{"id": "t0.r1.c1", "text": "가\n나"}, {"id": "t0.r0.c1", "text": "홍길동"}])
        old, new = hwp.HwpDoc(raw), self.reopen()
        old_recs, new_recs = old.sections[0]["recs"], new.sections[0]["recs"]
        self.assertEqual(sum(r[0] == hwp.PARA_HEADER for r in old_recs), sum(r[0] == hwp.PARA_HEADER for r in new_recs))
        keep = lambda recs: [r for r in recs if r[0] not in hwp.TEXT_KINDS]
        self.assertEqual(keep(old_recs), keep(new_recs))

    def test_newline_is_a_line_break_inside_one_paragraph(self):
        self.fill_ok([{"id": "t0.r0.c1", "text": "가\n나"}])
        doc = self.reopen()
        p = doc.targets["t0.r0.c1"][2]["paras"][0]
        self.assertEqual(p.text, "가\n나")
        recs = doc.sections[0]["recs"]
        units = struct.unpack("<%dH" % (len(p.payload) // 2), p.payload)
        self.assertEqual(units, (ord("가"), 10, ord("나"), 13))
        mask = struct.unpack_from("<I", recs[p.i][2], 4)[0]
        self.assertTrue(mask & (1 << 10))
        self.assertEqual(p.nchars, 4)

    def test_changed_paragraph_loses_line_seg_and_others_keep_it(self):
        raw, _ = self.fill_ok([{"id": "t0.r0.c1", "text": "홍길동"}])
        old, new = hwp.HwpDoc(raw), self.reopen()
        changed = new.targets["t0.r0.c1"][2]["paras"][0]
        self.assertNotIn(hwp.PARA_LINE_SEG, changed.kinds)
        untouched = new.targets["t0.r0.c0"][2]["paras"][0]
        self.assertIn(hwp.PARA_LINE_SEG, untouched.kinds)
        before = old.targets["t0.r0.c0"][2]["paras"][0]
        self.assertEqual(old.sections[0]["recs"][before.i:before.end], new.sections[0]["recs"][untouched.i:untouched.end])

    def test_tab_and_carriage_return_are_normalised(self):
        self.fill_ok([{"id": "t0.r0.c1", "text": "가\t나\r\n다\r라"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r0.c1"), ["가 나\n다\n라"])

    def test_append_to_empty_last_paragraph_has_no_leading_break(self):
        self.fill_ok([{"id": "t0.r1.c0", "text": "답변", "mode": "append"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c0"), ["지원동기(500자 이내)", "답변"])

    def test_append_after_text_adds_a_line_break(self):
        self.fill_ok([{"id": "t0.r1.c1", "text": "추가", "mode": "append"}, {"id": "t0.r0.c0", "text": "님", "mode": "append"}])
        doc = self.reopen()
        self.assertEqual(self.cell_texts(doc, "t0.r1.c1"), ["첫째", "둘째\n추가"])
        self.assertEqual(self.cell_texts(doc, "t0.r0.c0"), ["성명\n님"])
        p = doc.targets["t0.r0.c0"][2]["paras"][0]
        self.assertEqual(p.nchars, len("성명\n님") + 1)
        self.assertTrue(struct.unpack_from("<I", doc.sections[0]["recs"][p.i][2], 4)[0] & (1 << 10))

    def test_two_appends_to_one_cell_accumulate(self):
        self.fill_ok([{"id": "t0.r0.c0", "text": "하나", "mode": "append"}, {"id": "t0.r0.c0", "text": "둘", "mode": "append"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r0.c0"), ["성명\n하나\n둘"])

    def test_body_paragraph_is_replaced(self):
        raw, _ = self.fill_ok([{"id": "p1", "text": "새 제목\n둘째 줄"}])
        doc = self.reopen()
        self.assertEqual(doc.targets["p1"][2].text, "새 제목\n둘째 줄")
        self.assertEqual(len(doc.sections[0]["recs"]), len(hwp.HwpDoc(raw).sections[0]["recs"]) - 1)  # LINE_SEG 하나만 줄었다

    def test_unknown_and_unfillable_cells_are_skipped(self):
        raw, out = self.fill_ok([
            {"id": "t9.r0.c0", "text": "없는 표"}, {"id": "t0.r5.c5", "text": "없는 칸"}, {"id": "p99", "text": "없는 문단"},
            {"id": "t2.r0.c0", "text": "안쪽 표가 든 문단을 덮어쓰기"},   # 첫 문단에 개체가 있어 못 채움
            {"id": "p2", "text": "머리말이 달린 문단"},                    # 개체가 있어 못 채움
            {"id": "t0.r0.c1", "text": "😀"},                              # 담을 수 없는 글자
            {"id": "t0.r0.c1", "text": "홍길동"},
        ])
        self.assertIn("채운 칸 1개", out)
        self.assertIn("건너뜀: t9.r0.c0, t0.r5.c5, p99, t2.r0.c0, p2, t0.r0.c1", out)
        doc = self.reopen()
        self.assertEqual(self.cell_texts(doc, "t0.r0.c1"), ["홍길동"])
        self.assertEqual(self.cell_texts(doc, "t2.r0.c0"), ["", ""])

    def test_cell_after_nested_table_and_nested_cell_can_be_filled(self):
        self.fill_ok([{"id": "t2.r0.c1", "text": "바깥 칸"}, {"id": "t3.r0.c0", "text": "안쪽 칸"},
                      {"id": "t1.r0.c0", "text": "머리말 칸"}, {"id": "t2.r0.c0", "text": "덧붙임", "mode": "append"}])
        doc = self.reopen()
        self.assertEqual(self.cell_texts(doc, "t2.r0.c1"), ["바깥 칸"])
        self.assertEqual(self.cell_texts(doc, "t3.r0.c0"), ["안쪽 칸"])
        self.assertEqual(self.cell_texts(doc, "t1.r0.c0"), ["머리말 칸"])
        self.assertEqual(self.cell_texts(doc, "t2.r0.c0"), ["", "덧붙임"])

    def test_paragraph_replace_keeps_other_paragraphs_of_the_cell(self):
        raw, out = self.fill_ok([{"id": "t0.r1.c1.p1", "text": "새 둘째"}])
        self.assertIn("채운 칸 1개", out)
        self.assertNotIn("건너뜀", out)
        old, new = hwp.HwpDoc(raw), self.reopen()
        self.assertEqual(self.cell_texts(new, "t0.r1.c1"), ["첫째", "새 둘째"])
        kept_old, kept_new = old.para_targets["t0.r1.c1.p0"][1], new.para_targets["t0.r1.c1.p0"][1]
        self.assertEqual(old.sections[0]["recs"][kept_old.i:kept_old.end], new.sections[0]["recs"][kept_new.i:kept_new.end])

    def test_paragraph_replace_on_first_paragraph_leaves_the_rest(self):
        self.fill_ok([{"id": "t0.r1.c1.p0", "text": "새 첫째"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c1"), ["새 첫째", "둘째"])

    def test_two_paragraphs_of_one_cell_can_be_filled_together(self):
        _, out = self.fill_ok([{"id": "t0.r1.c1.p0", "text": "가"}, {"id": "t0.r1.c1.p1", "text": "나"}])
        self.assertIn("채운 칸 2개", out)
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c1"), ["가", "나"])

    def test_paragraph_replace_with_line_break_stays_one_paragraph(self):
        self.fill_ok([{"id": "t0.r1.c1.p1", "text": "가\n나"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c1"), ["첫째", "가\n나"])

    def test_paragraph_replace_on_single_paragraph_cell(self):
        self.fill_ok([{"id": "t0.r0.c1.p0", "text": "홍길동"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r0.c1"), ["홍길동"])

    def test_paragraph_append_after_text_adds_a_line_break(self):
        self.fill_ok([{"id": "t0.r1.c1.p0", "text": "추가", "mode": "append"}])
        doc = self.reopen()
        self.assertEqual(self.cell_texts(doc, "t0.r1.c1"), ["첫째\n추가", "둘째"])
        p = doc.para_targets["t0.r1.c1.p0"][1]
        self.assertEqual(p.nchars, len("첫째\n추가") + 1)

    def test_paragraph_append_to_empty_paragraph_has_no_leading_break(self):
        self.fill_ok([{"id": "t0.r1.c0.p1", "text": "답변", "mode": "append"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c0"), ["지원동기(500자 이내)", "답변"])

    def test_two_paragraph_appends_accumulate(self):
        self.fill_ok([{"id": "t0.r1.c1.p1", "text": "하나", "mode": "append"}, {"id": "t0.r1.c1.p1", "text": "둘", "mode": "append"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c1"), ["첫째", "둘째\n하나\n둘"])

    def test_paragraph_replace_then_append_in_one_run(self):
        self.fill_ok([{"id": "t0.r1.c1.p1", "text": "바꿈"}, {"id": "t0.r1.c1.p1", "text": "덧붙임", "mode": "append"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c1"), ["첫째", "바꿈\n덧붙임"])

    def test_cell_and_paragraph_answers_for_one_cell_skip_the_cell_answer(self):
        _, out = self.fill_ok([{"id": "t0.r1.c1", "text": "칸 전체"}, {"id": "t0.r1.c1.p1", "text": "문단만"}])
        self.assertIn("채운 칸 1개", out)
        self.assertIn("건너뜀: t0.r1.c1 (같은 칸의 문단 번호 답이 있어 건너뜀)", out)
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c1"), ["첫째", "문단만"])

    def test_conflict_is_skipped_whatever_the_answer_order(self):
        _, out = self.fill_ok([{"id": "t0.r1.c1.p1", "text": "문단만"}, {"id": "t0.r1.c1", "text": "칸 전체", "mode": "append"}])
        self.assertIn("t0.r1.c1 (같은 칸의 문단 번호 답이 있어 건너뜀)", out)
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c1"), ["첫째", "문단만"])

    def test_conflict_only_affects_the_same_cell(self):
        self.fill_ok([{"id": "t0.r1.c1", "text": "칸 전체"}, {"id": "t0.r1.c0.p1", "text": "문단만"}])
        doc = self.reopen()
        self.assertEqual(self.cell_texts(doc, "t0.r1.c1"), ["칸 전체", ""])
        self.assertEqual(self.cell_texts(doc, "t0.r1.c0"), ["지원동기(500자 이내)", "문단만"])

    def test_cell_answer_alone_still_clears_other_paragraphs(self):
        self.fill_ok([{"id": "t0.r1.c1", "text": "칸 전체"}])
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c1"), ["칸 전체", ""])

    def test_missing_paragraph_number_is_skipped_and_cell_answer_too(self):
        _, out = self.fill_ok([{"id": "t0.r1.c1.p9", "text": "없는 문단"}, {"id": "t0.r1.c1", "text": "칸 전체"},
                               {"id": "t0.r0.c1.p0", "text": "홍길동"}])
        self.assertIn("채운 칸 1개", out)
        self.assertIn("건너뜀: t0.r1.c1.p9, t0.r1.c1 (같은 칸의 문단 번호 답이 있어 건너뜀)", out)
        self.assertEqual(self.cell_texts(self.reopen(), "t0.r1.c1"), ["첫째", "둘째"])

    def test_paragraph_with_object_is_skipped_but_its_sibling_is_filled(self):
        _, out = self.fill_ok([{"id": "t2.r0.c0.p0", "text": "개체 문단"}, {"id": "t2.r0.c0.p1", "text": "빈 문단"},
                               {"id": "t0.r1.c1.p0", "text": "😀"}])
        self.assertIn("채운 칸 1개", out)
        self.assertIn("건너뜀: t2.r0.c0.p0, t0.r1.c1.p0", out)
        self.assertEqual(self.cell_texts(self.reopen(), "t2.r0.c0"), ["", "빈 문단"])

    def test_paragraph_fill_passes_self_check_with_one_changed_paragraph(self):
        raw = self.install(make_hwp(compressed=self.compressed))
        new_raw, applied, unknown, _, changed = hwp.fill(raw, [("t0.r1.c1.p1", "새 둘째", "replace")])
        self.assertEqual((applied, unknown), (["t0.r1.c1.p1"], []))
        plan = changed["BodyText/Section0"]
        self.assertEqual(len(plan["touched"]), 1)
        self.assertEqual(hwp.verify(raw, new_raw, changed), [])

    def test_self_check_catches_a_cell_paragraph_changed_beyond_the_target(self):
        raw = self.install(make_hwp(compressed=self.compressed))
        new_raw, _, _, _, changed = hwp.fill(raw, [("t0.r1.c1.p1", "새 둘째", "replace")])
        other = hwp.HwpDoc(raw).para_targets["t0.r1.c1.p0"][1].ordinal
        changed["BodyText/Section0"]["touched"].add(other)  # 같은 칸의 다른 문단도 바뀌었다고 주장
        self.assertTrue(any(m.startswith("4.") for m in hwp.verify(raw, new_raw, changed)))

    def test_only_changed_streams_are_rewritten(self):
        raw, _ = self.fill_ok([{"id": "t0.r0.c1", "text": "홍길동"}])
        old, new = hwp.Cfb(raw), hwp.Cfb(self.output().read_bytes())
        self.assertEqual(sorted(old.streams), sorted(new.streams))
        for name in old.streams:
            if name != "BodyText/Section0":
                self.assertEqual(old.read(name), new.read(name), name)
        self.assertNotEqual(old.read("BodyText/Section0"), new.read("BodyText/Section0"))

    def test_only_the_section_with_the_cell_is_rewritten(self):
        second = para(0, "", [lambda lv: table(lv, [(0, 0, 1, 1, ["둘째 구역 칸"])])]) + filler(80, seed=2)
        raw, _ = self.fill_ok([{"id": "t4.r0.c0", "text": "채움"}], sections=[section_body(), second])
        old, new = hwp.Cfb(raw), hwp.Cfb(self.output().read_bytes())
        self.assertEqual(old.read("BodyText/Section0"), new.read("BodyText/Section0"))
        self.assertNotEqual(old.read("BodyText/Section1"), new.read("BodyText/Section1"))
        self.assertEqual(self.cell_texts(self.reopen(), "t4.r0.c0"), ["채움"])

    def test_output_name_is_not_overwritten(self):
        self.fill_ok([{"id": "t0.r0.c1", "text": "하나"}])
        first = self.output().read_bytes()
        code, _ = self.run_cmd("fill")
        self.assertEqual(code, 0)
        self.assertEqual(self.output().read_bytes(), first)
        self.assertTrue(self.output("지원서-작성-2.hwp").exists())

    def test_original_form_is_not_modified(self):
        raw, _ = self.fill_ok([{"id": "t0.r0.c1", "text": "홍길동"}])
        self.assertEqual((self.root / "forms" / "지원서.hwp").read_bytes(), raw)

    def test_small_section_is_refused_without_leaving_output(self):
        self.prepare([{"id": "t0.r0.c1", "text": "홍길동"}], filler_count=0)
        code, out = self.run_cmd("fill")
        self.assertEqual(code, 1)
        self.assertIn("4096", out)
        self.assertFalse((self.root / "applications" / "회사-직무").exists() and any((self.root / "applications" / "회사-직무").iterdir()))


class FillCompressedTest(FillTest):
    compressed = True

    def test_result_is_still_compressed_and_readable(self):
        raw, _ = self.fill_ok([{"id": "t0.r0.c1", "text": "홍길동"}])
        new = hwp.Cfb(self.output().read_bytes())
        self.assertEqual(new.read("FileHeader"), hwp.Cfb(raw).read("FileHeader"))
        self.assertTrue(hwp.is_compressed(new))
        data = hwp.inflate(new.read("BodyText/Section0"))
        self.assertEqual(hwp.build_records(hwp.parse_records(data)), data)

    def test_small_compressed_result_uses_stored_blocks(self):
        # hwppatch 와 같은 규칙: 압축 결과가 4096바이트 미만이면 무압축 블록으로 쓴다
        data = b"\1" * 6000
        self.assertGreaterEqual(len(hwp.deflate(data)), 6000)
        self.assertEqual(zlib.decompress(hwp.deflate(data), -15), data)
        noisy = random.Random(1).randbytes(9000)
        self.assertEqual(hwp.deflate(noisy), deflate_raw(noisy))


# ---------- 자체 검수 ----------

class SelfCheckTest(HwpBase):
    def filled(self):
        raw = make_hwp()
        doc_answers = [("t0.r0.c1", "홍길동", "replace"), ("t0.r1.c1", "가\n나", "replace")]
        new, applied, unknown, _, changed = hwp.fill(raw, doc_answers)
        self.assertEqual((applied, unknown), (["t0.r0.c1", "t0.r1.c1"], []))
        return raw, new, changed

    def test_passes_for_good_result(self):
        raw, new, changed = self.filled()
        self.assertEqual(hwp.verify(raw, new, changed), [])

    def test_item1_stream_list_differs(self):
        raw, new, changed = self.filled()
        other = make_hwp(extra={"Extra": b"x"})
        self.assertTrue(hwp.verify(other, new, changed)[0].startswith("1."))

    def test_item2_other_stream_changed(self):
        raw, new, changed = self.filled()
        tampered = hwp.rebuild_ole(new, "DocInfo", b"\7" * 5000)
        self.assertTrue(any(m.startswith("2.") and "DocInfo" in m for m in hwp.verify(raw, tampered, changed)))

    def test_item3_non_text_record_changed(self):
        raw, new, changed = self.filled()
        cfb = hwp.Cfb(new)
        recs = hwp.parse_records(cfb.read("BodyText/Section0"))
        k = next(i for i, r in enumerate(recs) if r[0] == hwp.LIST_HEADER)
        recs[k][2] = recs[k][2][:12] + b"\x09\x00" + recs[k][2][14:]
        tampered = hwp.rebuild_ole(new, "BodyText/Section0", hwp.build_records(recs))
        self.assertTrue(any(m.startswith("3.") for m in hwp.verify(raw, tampered, changed)))

    def test_item4_extra_paragraph_changed(self):
        raw, new, changed = self.filled()
        recs = hwp.parse_records(hwp.Cfb(new).read("BodyText/Section0"))
        k = next(i for i, r in enumerate(recs) if r[0] == hwp.PARA_LINE_SEG)  # 대상이 아닌 첫 문단의 줄 배치
        del recs[k]
        tampered = hwp.rebuild_ole(new, "BodyText/Section0", hwp.build_records(recs))
        self.assertTrue(any(m.startswith("4.") for m in hwp.verify(raw, tampered, changed)))

    def test_item4_missing_change(self):
        raw, new, changed = self.filled()
        changed["BodyText/Section0"]["touched"].add(0)
        self.assertTrue(any(m.startswith("4.") for m in hwp.verify(raw, new, changed)))

    def test_item5_text_differs(self):
        raw, new, changed = self.filled()
        for ordinal in changed["BodyText/Section0"]["expected"]:
            changed["BodyText/Section0"]["expected"][ordinal] += "!"
        self.assertTrue(any(m.startswith("5.") for m in hwp.verify(raw, new, changed)))

    def test_failed_check_removes_output_and_reports_item(self):
        self.install(make_hwp())
        self.answer([{"id": "t0.r0.c1", "text": "홍길동"}])
        with mock.patch.object(hwp, "verify", return_value=["4. 문단 수가 다름", "5. 글자가 다름"]):
            code, out = self.run_cmd("fill")
        self.assertEqual(code, 1)
        self.assertIn("4. 문단 수가 다름", out)
        self.assertIn("5. 글자가 다름", out)
        self.assertFalse(self.output().exists())
        self.assertEqual(list((self.root / "applications" / "회사-직무").glob("*")), [])


# ---------- 사진·칸 높이·미리보기 ----------

def jpeg_bytes(w, h, sof=0xC0, size=6000, seed=1):
    out = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\1\1\0\0\1\0\1\0\0"
    out += b"\xff\xff" + bytes([0xFF, sof]) + struct.pack(">HBHHB", 17, 8, h, w, 3) + b"\1\x22\0\2\x11\1\3\x11\1"
    out += b"\xff\xda" + struct.pack(">H", 12) + b"\3\1\0\2\x11\3\x11\0\x3f\0"
    return out + random.Random(seed).randbytes(size) + b"\xff\xd9"


def png_chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png_bytes(w, h, size=6000, seed=2):
    ihdr = png_chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    return b"\x89PNG\r\n\x1a\n" + ihdr + png_chunk(b"IDAT", random.Random(seed).randbytes(size)) + png_chunk(b"IEND", b"")


def docinfo_bytes(bins=2, filler=b""):
    """그림 개수(ID_MAPPINGS)와 BIN_DATA, 글자 모양 8개, 문단 모양 둘(0: 가운데 정렬 120%, 1: 고정 값)이 든 DocInfo."""
    out = rec(16, 0, b"\0" * 26) + rec(17, 0, struct.pack("<I", bins) + b"\0" * 60)
    for n in range(bins):
        out += rec(18, 1, struct.pack("<HHH", 1, n + 1, 3) + "gif".encode("utf-16le"))
    char = b"\0" * 42 + struct.pack("<i", 1000) + struct.pack("<I", 0) + b"\0" * 22
    out += b"".join(rec(21, 1, char) for _ in range(8))
    shape = lambda attr, spacing: struct.pack("<I", attr) + b"\0" * 20 + struct.pack("<i", spacing) + b"\0" * 18
    out += rec(25, 1, shape(0x0C, 120)) + rec(25, 1, shape(0x01, 4000))
    return out + (rec(80, 1, filler) if filler else b"")


def sized_section(extra=b""):
    """칸 높이 시험용: 표 0 은 높이가 있는 두 열 세 행, 표 1 은 세로로 합친 칸이 있다."""
    return (para(0, "입사지원서")
            + para(0, "", [lambda lv: table(lv, [
                (0, 0, 1, 1, ["가"], 4000, 3000), (0, 1, 1, 1, ["나"], 4000, 3200),
                (1, 0, 1, 1, ["다"], 4000, 4000), (1, 1, 1, 1, ["라"], 4000, 4000),
                (2, 0, 1, 1, ["마"], 4000, 5000), (2, 1, 1, 1, ["바"], 4000, 5000)])])
            + para(0, "", [lambda lv: table(lv, [
                (0, 0, 1, 2, ["합친"], 4000, 6000), (0, 1, 1, 1, ["위"], 4000, 3000), (1, 1, 1, 1, ["아래"], 4000, 3000)])])
            + extra + filler(80))


def make_photo_hwp(compressed=True, filler_bytes=b"", bins=2, red=True, extra_streams=None, fat=3, section=None):
    section = section or section_body()
    header = b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000300, 1 if compressed else 0)
    info = docinfo_bytes(bins, filler_bytes)
    streams = {"FileHeader": header.ljust(256, b"\0"), "DocInfo": deflate_raw(info) if compressed else info,
               "PrvText": "미리보기".encode("utf-16le"),
               "BodyText/Section0": deflate_raw(section) if compressed else section,
               "BinData/BIN0001.gif": random.Random(11).randbytes(5000), "BinData/BIN0002.gif": random.Random(12).randbytes(5000)}
    streams.update(extra_streams or {})
    return make_cfb(streams, min_fat_sectors=fat, red={"BinData/BIN0002.gif"} if red else ())


class PhotoBase(HwpBase):
    PHOTO = "photos/증명사진.jpg"

    def put_file(self, data, name=PHOTO):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def answer_all(self, answers=(), heights=None):
        data = {"folder": "회사-직무", "answers": list(answers)}
        if heights is not None:
            data["heights"] = heights
        (self.root / "work" / "form-answers.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def photo_item(self, cid="t0.r0.c1", image=PHOTO, w=30, h=40):
        return {"id": cid, "image": image, "width_mm": w, "height_mm": h}

    def fill_ok(self, raw=None, answers=(), heights=None, **kw):
        self.install(raw if raw is not None else make_photo_hwp(**kw))
        self.answer_all(answers, heights)
        code, out = self.run_cmd("fill")
        self.assertEqual(code, 0, out)
        return out

    def fill_fails(self, raw, answers=(), heights=None):
        self.install(raw)
        self.answer_all(answers, heights)
        code, out = self.run_cmd("fill")
        self.assertEqual(code, 1, out)
        self.assertEqual(sorted((self.root / "applications" / "회사-직무").glob("*")) if (self.root / "applications" / "회사-직무").exists() else [], [])
        return out

    def new_cfb(self):
        return hwp.Cfb(self.output().read_bytes())

    def records(self, cfb, name, compressed=True):
        data = cfb.read(name)
        return hwp.parse_records(hwp.inflate(data) if compressed else data)


class ImageHeaderTest(unittest.TestCase):
    def test_reads_jpeg_size_from_the_header(self):
        self.assertEqual(hwp.image_info(jpeg_bytes(354, 472)), ("jpg", 354, 472))

    def test_reads_progressive_jpeg_and_skips_fill_bytes_and_other_segments(self):
        self.assertEqual(hwp.image_info(jpeg_bytes(100, 200, sof=0xC2)), ("jpg", 100, 200))

    def test_reads_png_size_from_ihdr(self):
        self.assertEqual(hwp.image_info(png_bytes(354, 472)), ("png", 354, 472))

    def test_rejects_other_formats(self):
        for data in (b"GIF89a" + b"\0" * 40, b"BM" + b"\0" * 40, b"RIFF\0\0\0\0WEBPVP8 " + b"\0" * 30, b"", b"hello world" * 5):
            with self.assertRaises(_common.UserError) as ctx:
                hwp.image_info(data)
            self.assertIn("JPEG", str(ctx.exception))

    def test_rejects_broken_jpeg_headers(self):
        no_frame = b"\xff\xd8\xff\xe0" + struct.pack(">H", 16) + b"JFIF\0\1\1\0\0\1\0\1\0\0" + b"\xff\xda\0\2"
        zero_height = jpeg_bytes(100, 0)
        for data in (no_frame, zero_height, jpeg_bytes(100, 100)[:25]):
            with self.assertRaises(_common.UserError):
                hwp.image_info(data)

    def test_rejects_broken_png_headers(self):
        good = png_bytes(10, 10)
        wrong_crc = good[:29] + b"\0\0\0\0" + good[33:]
        zero_width = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 0, 5, 8, 2, 0, 0, 0))
        not_ihdr = b"\x89PNG\r\n\x1a\n" + png_chunk(b"IDAT", b"\0" * 13)
        for data in (wrong_crc, zero_width, not_ihdr, good[:20]):
            with self.assertRaises(_common.UserError):
                hwp.image_info(data)

    @unittest.skipUnless(hasattr(sys, "stdlib_module_names"), "Python 3.10 이상에서만 표준 라이브러리 목록을 알 수 있음")
    def test_scripts_use_only_the_standard_library(self):
        import ast
        scripts = Path(hwp.__file__).parent
        for name in ("hwp.py", "form_tool.py"):
            tree = ast.parse((scripts / name).read_text(encoding="utf-8"))
            modules = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            modules |= {n.module.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
            self.assertEqual(sorted(m for m in modules if m not in sys.stdlib_module_names and m not in ("_common", "hwp")), [], name)


class PhotoInputTest(PhotoBase):
    def test_accepts_relative_and_absolute_paths_under_the_root(self):
        self.put_file(jpeg_bytes(354, 472))
        self.fill_ok(answers=[self.photo_item()])
        self.output().unlink()
        self.fill_ok(answers=[self.photo_item(image=str(self.root / self.PHOTO))])
        self.assertTrue(self.output().exists())

    def test_rejects_path_outside_the_root(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other) / "photo.jpg"
            outside.write_bytes(jpeg_bytes(354, 472))
            out = self.fill_fails(make_photo_hwp(), [self.photo_item(image=str(outside))])
        self.assertIn("작업 폴더 안", out)

    def test_rejects_dotdot_in_path(self):
        self.put_file(jpeg_bytes(354, 472))
        out = self.fill_fails(make_photo_hwp(), [self.photo_item(image="photos/../photos/증명사진.jpg")])
        self.assertIn("..", out)

    def test_rejects_symlink_to_a_file_inside_the_root(self):
        real = self.put_file(jpeg_bytes(354, 472), "photos/real.jpg")
        (self.root / "photos" / "link.jpg").symlink_to(real)
        out = self.fill_fails(make_photo_hwp(), [self.photo_item(image="photos/link.jpg")])
        self.assertIn("심볼릭 링크", out)

    def test_rejects_symlinked_folder_on_the_way(self):
        with tempfile.TemporaryDirectory() as other:
            (Path(other) / "photo.jpg").write_bytes(jpeg_bytes(354, 472))
            (self.root / "linked").symlink_to(other)
            out = self.fill_fails(make_photo_hwp(), [self.photo_item(image="linked/photo.jpg")])
        self.assertIn("심볼릭 링크", out)

    def test_rejects_missing_file_and_folder(self):
        (self.root / "photos").mkdir()
        for image in ("photos/없음.jpg", "photos", ""):
            out = self.fill_fails(make_photo_hwp(), [self.photo_item(image=image)])
            self.assertIn("오류:", out)

    def test_rejects_other_formats_by_content_not_by_name(self):
        self.put_file(b"GIF89a" + b"\0" * 6000, "photos/가짜.jpg")
        out = self.fill_fails(make_photo_hwp(), [self.photo_item(image="photos/가짜.jpg")])
        self.assertIn("JPEG 와 PNG", out)

    def test_png_content_is_accepted_even_with_a_jpg_name(self):
        self.put_file(png_bytes(200, 300), "photos/사진.jpg")
        self.fill_ok(answers=[self.photo_item(image="photos/사진.jpg")])
        self.assertIn("BinData/BIN0003.png", self.new_cfb().streams)

    def test_rejects_more_than_one_photo(self):
        self.put_file(jpeg_bytes(354, 472))
        out = self.fill_fails(make_photo_hwp(), [self.photo_item(), self.photo_item("t0.r1.c1")])
        self.assertIn("한 장", out)

    def test_rejects_image_item_with_text_or_without_size(self):
        self.put_file(jpeg_bytes(354, 472))
        for item in (dict(self.photo_item(), text="x"), {"id": "t0.r0.c1", "image": self.PHOTO},
                     dict(self.photo_item(), width_mm="30"), dict(self.photo_item(), width_mm=True)):
            out = self.fill_fails(make_photo_hwp(), [item])
            self.assertIn("answers[0]", out)

    def test_rejects_bad_size_values(self):
        self.put_file(jpeg_bytes(354, 472))
        for w, h in ((0, 40), (-3, 40), (30, 5000), (float("nan"), 40)):
            out = self.fill_fails(make_photo_hwp(), [self.photo_item(w=w, h=h)])
            self.assertIn("오류:", out)

    def test_photo_cell_must_be_a_cell_id_and_exist(self):
        self.put_file(jpeg_bytes(354, 472))
        for cid in ("t0.r0.c1.p0", "p0", "t0.r9.c9"):
            self.fill_fails(make_photo_hwp(), [self.photo_item(cid)])

    def test_text_answer_for_the_photo_cell_is_refused(self):
        self.put_file(jpeg_bytes(354, 472))
        for aid in ("t0.r0.c1", "t0.r0.c1.p0"):
            out = self.fill_fails(make_photo_hwp(), [{"id": aid, "text": "x"}, self.photo_item()])
            self.assertIn("같은 칸에 글과 사진", out)

    def test_cell_with_other_objects_is_refused(self):
        self.put_file(jpeg_bytes(354, 472))
        out = self.fill_fails(make_photo_hwp(), [self.photo_item("t2.r0.c0")])
        self.assertIn("다른 개체", out)


class PhotoFillTest(PhotoBase):
    def setUp(self):
        super().setUp()
        self.photo = jpeg_bytes(354, 472)
        self.put_file(self.photo)

    def check_picture(self, raw, compressed=True, cid="t0.r0.c1", data=None, ext="jpg", mm=(30, 40)):
        """결과 파일에서 그림 칸·DocInfo·BinData 가 계획대로인지 본다."""
        old, new = hwp.Cfb(raw), self.new_cfb()
        doc = self.reopen()
        _, _, cell = doc.targets[cid]
        self.assertEqual(len(cell["paras"]), 1)
        recs = doc.sections[0]["recs"]
        span = recs[cell["head"] + 1:cell["paras"][0].end]
        self.assertEqual([r[0] for r in span], [hwp.PARA_HEADER, hwp.PARA_TEXT, hwp.PARA_CHAR_SHAPE, hwp.CTRL_HEADER, 76, 85])
        self.assertEqual(struct.unpack_from("<II", span[3][2], 16), tuple(round(v / 25.4 * 7200) for v in mm))
        self.assertEqual(struct.unpack_from("<H", span[5][2], 71)[0], 3)
        self.assertEqual(sorted(new.streams), sorted(list(old.streams) + [f"BinData/BIN0003.{ext}"]))
        stored = new.read(f"BinData/BIN0003.{ext}")
        self.assertEqual(hwp.inflate(stored) if compressed else stored, data or self.photo)
        info_old, info_new = self.records(old, "DocInfo", compressed), self.records(new, "DocInfo", compressed)
        self.assertEqual(len(info_new), len(info_old) + 1)
        self.assertEqual(struct.unpack_from("<I", next(r for r in info_new if r[0] == 17)[2], 0)[0], 3)
        self.assertEqual([r[2] for r in info_new if r[0] == 18][-1], struct.pack("<HHH", 1, 3, 3) + ext.encode("utf-16le"))
        changed = [n for n in old.streams if old.read(n) != new.read(n)]
        self.assertEqual(sorted(changed), ["BodyText/Section0", "DocInfo"])

    def test_photo_goes_into_the_cell_as_inline_picture(self):
        raw = make_photo_hwp()
        out = self.fill_ok(raw, [self.photo_item()])
        self.assertIn("사진: t0.r0.c1 칸에 넣었습니다", out)
        self.check_picture(raw)

    def test_photo_in_uncompressed_document(self):
        raw = make_photo_hwp(compressed=False)
        self.fill_ok(raw, [self.photo_item()])
        self.check_picture(raw, compressed=False)

    def test_photo_when_docinfo_is_a_regular_stream(self):
        raw = make_photo_hwp(filler_bytes=random.Random(5).randbytes(5000))
        self.assertGreaterEqual(hwp.Cfb(raw).streams["DocInfo"]["size"], 4096)
        self.fill_ok(raw, [self.photo_item()])
        self.check_picture(raw)

    def test_docinfo_moves_out_of_the_mini_stream_when_it_does_not_fit(self):
        raw = make_photo_hwp(filler_bytes=bytes(6000))
        self.assertLess(hwp.Cfb(raw).streams["DocInfo"]["size"], 4096)
        with mock.patch.object(hwp, "overwrite_mini", side_effect=_common.UserError("모자람")):
            self.fill_ok(raw, [self.photo_item()])
        self.assertGreaterEqual(self.new_cfb().streams["DocInfo"]["size"], 4096)
        self.check_picture(raw)

    def test_png_photo_gets_a_png_stream(self):
        data = png_bytes(300, 400)
        self.put_file(data, "photos/사진.png")
        raw = make_photo_hwp()
        self.fill_ok(raw, [self.photo_item(image="photos/사진.png")])
        self.check_picture(raw, data=data, ext="png")

    def test_picture_size_follows_the_millimetres(self):
        raw = make_photo_hwp()
        self.fill_ok(raw, [self.photo_item(w=25.5, h=34)])
        self.check_picture(raw, mm=(25.5, 34))

    def test_existing_text_and_paragraphs_of_the_cell_are_replaced(self):
        raw = make_photo_hwp()
        self.assertEqual(len(hwp.HwpDoc(raw).targets["t0.r1.c0"][2]["paras"]), 2)
        self.fill_ok(raw, [self.photo_item("t0.r1.c0")])
        self.check_picture(raw, cid="t0.r1.c0")

    def test_text_edits_before_and_after_the_photo_cell_keep_their_paragraph_numbers(self):
        raw = make_photo_hwp()
        self.fill_ok(raw, [{"id": "t0.r0.c1", "text": "앞 글"}, self.photo_item("t0.r1.c0"),
                           {"id": "t0.r1.c1", "text": "뒤 글\n둘째"}, {"id": "t2.r0.c1", "text": "다른 표"}])
        doc = self.reopen()
        self.assertEqual(self.cell_texts(doc, "t0.r0.c1"), ["앞 글"])
        self.assertEqual(self.cell_texts(doc, "t0.r1.c1"), ["뒤 글\n둘째", ""])
        self.assertEqual(self.cell_texts(doc, "t2.r0.c1"), ["다른 표"])
        self.assertEqual(len(doc.targets["t0.r1.c0"][2]["paras"]), 1)

    def test_text_then_heights_then_photo_make_one_result_file(self):
        raw = make_photo_hwp(section=sized_section())
        self.fill_ok(raw, [{"id": "t0.r0.c0", "text": "새 글"}, self.photo_item("t0.r2.c1")],
                     [{"id": "t0.r0.c0", "height_mm": 3500 / 7200 * 25.4}, {"id": "t0.r1.c0", "height_mm": 3700 / 7200 * 25.4}])
        files = sorted(p.name for p in (self.root / "applications" / "회사-직무").glob("*"))
        self.assertEqual(files, ["지원서-작성-미리보기.html", "지원서-작성.hwp"])
        doc = self.reopen()
        self.assertEqual(self.cell_texts(doc, "t0.r0.c0"), ["새 글"])
        recs = doc.sections[0]["recs"]
        heights = {cid: struct.unpack_from("<I", recs[doc.targets[cid][2]["head"]][2], 20)[0]
                   for cid in ("t0.r0.c0", "t0.r0.c1", "t0.r1.c0", "t0.r1.c1", "t0.r2.c0")}
        self.assertEqual(heights, {"t0.r0.c0": 3500, "t0.r0.c1": 3500, "t0.r1.c0": 3700, "t0.r1.c1": 3700, "t0.r2.c0": 5000})
        self.assertEqual(len(doc.targets["t0.r2.c1"][2]["paras"]), 1)

    def test_same_input_gives_same_bytes(self):
        raw = make_photo_hwp()
        results = []
        for _ in range(2):
            results.append(hwp.fill(raw, [], (), {"id": "t0.r0.c1", "data": self.photo, "width_mm": 30, "height_mm": 40})[0])
        self.assertEqual(results[0], results[1])

    def test_directory_and_fat_sectors_are_appended_when_needed(self):
        extra = {f"Extra{n}": b"e" * 10 for n in range(3)}  # 디렉터리 항목이 4의 배수로 꽉 참
        raw = make_photo_hwp(extra_streams=extra, fat=1)
        cfb = hwp.Cfb(raw)
        self.assertFalse(any(e["type"] == 0 for e in cfb.entries))
        self.assertEqual(len(cfb.difat), 1)
        self.put_file(jpeg_bytes(354, 472, size=70000), self.PHOTO)
        self.photo = (self.root / self.PHOTO).read_bytes()
        self.fill_ok(raw, [self.photo_item()])
        new = self.new_cfb()
        self.assertGreater(len(new.difat), 1)
        self.assertGreater(len(new.entries), len(cfb.entries))
        self.check_picture(raw)

    def test_refuses_a_photo_too_small_for_a_regular_stream(self):
        self.put_file(jpeg_bytes(354, 472, size=100))
        out = self.fill_fails(make_photo_hwp(), [self.photo_item()])
        self.assertIn("너무 작아", out)

    def test_refuses_when_docinfo_picture_count_and_streams_disagree(self):
        out = self.fill_fails(make_photo_hwp(bins=3), [self.photo_item()])
        self.assertIn("그림 개수", out)

    def test_refuses_an_unexpected_bindata_tree_shape(self):
        out = self.fill_fails(make_photo_hwp(red=False), [self.photo_item()])
        self.assertIn("배치가 예상과 달라", out)

    def test_docx_and_hwpx_list_the_new_fields_as_skipped_and_fill_the_rest(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import test_form_tool as tf
        for kind in ("docx", "hwpx"):
            for old in (self.root / "forms").glob("*"):
                old.unlink()
            path = self.root / "forms" / f"양식.{kind}"
            (tf.make_docx if kind == "docx" else tf.make_hwpx)(path)
            self.answer_all([{"id": "t0.r0.c1", "text": "홍길동"}, self.photo_item("t0.r0.c0")], [{"id": "t0.r0.c0", "height_mm": 20}])
            code, out = self.run_cmd("fill")
            self.assertEqual(code, 0, out)
            self.assertIn("채운 칸 1개", out)
            self.assertIn("t0.r0.c0 (사진은 .hwp 양식에만 넣을 수 있음)", out)
            self.assertIn("t0.r0.c0 (칸 높이 바꾸기는 .hwp 양식에만 쓸 수 있음)", out)
            self.assertTrue((self.root / "applications" / "회사-직무" / f"양식-작성.{kind}").exists())
            self.assertEqual(list((self.root / "applications" / "회사-직무").glob("*.html")), [])
            for made in (self.root / "applications" / "회사-직무").glob("*"):
                made.unlink()


class HeightsTest(PhotoBase):
    def units(self, n):
        return n / 7200 * 25.4

    def heights_of(self, doc, ids):
        recs = doc.sections[0]["recs"]
        return {cid: struct.unpack_from("<I", recs[doc.targets[cid][2]["head"]][2], 20)[0] for cid in ids}

    def test_changes_only_the_height_bytes_of_every_cell_in_the_row(self):
        raw = make_photo_hwp(section=sized_section())
        self.fill_ok(raw, heights=[{"id": "t0.r0.c0", "height_mm": self.units(3500)}, {"id": "t0.r1.c1", "height_mm": self.units(3700)}])
        old_recs = hwp.HwpDoc(raw).sections[0]["recs"]
        new_recs = self.reopen().sections[0]["recs"]
        self.assertEqual(len(old_recs), len(new_recs))
        diffs = [(i, [k for k in range(len(a[2])) if a[2][k] != b[2][k]]) for i, (a, b) in enumerate(zip(old_recs, new_recs)) if a != b]
        self.assertTrue(diffs)
        for i, positions in diffs:
            self.assertEqual(old_recs[i][0], hwp.LIST_HEADER)
            self.assertTrue(set(positions) <= {20, 21, 22, 23}, positions)
        doc = self.reopen()
        self.assertEqual(self.heights_of(doc, ["t0.r0.c0", "t0.r0.c1", "t0.r1.c0", "t0.r1.c1", "t0.r2.c0", "t0.r2.c1"]),
                         {"t0.r0.c0": 3500, "t0.r0.c1": 3500, "t0.r1.c0": 3700, "t0.r1.c1": 3700, "t0.r2.c0": 5000, "t0.r2.c1": 5000})

    def test_other_streams_stay_byte_for_byte(self):
        raw = make_photo_hwp(section=sized_section())
        self.fill_ok(raw, heights=[{"id": "t0.r0.c0", "height_mm": self.units(3500)}, {"id": "t0.r1.c0", "height_mm": self.units(3700)}])
        old, new = hwp.Cfb(raw), self.new_cfb()
        self.assertEqual(sorted(old.streams), sorted(new.streams))
        self.assertEqual([n for n in old.streams if old.read(n) != new.read(n)], ["BodyText/Section0"])

    def test_row_sum_change_is_refused_with_table_number_and_difference(self):
        out = self.fill_fails(make_photo_hwp(section=sized_section()),
                              heights=[{"id": "t0.r0.c0", "height_mm": self.units(3300)}, {"id": "t0.r1.c0", "height_mm": self.units(3700)}])
        self.assertIn("표 0", out)
        self.assertIn("-200", out)
        self.assertIn("7200", out)
        self.assertIn("7000", out)

    def test_row_sum_is_checked_per_table(self):
        out = self.fill_fails(make_photo_hwp(section=sized_section()),
                              heights=[{"id": "t0.r0.c0", "height_mm": self.units(3500)}, {"id": "t0.r1.c0", "height_mm": self.units(3700)},
                                       {"id": "t1.r0.c1", "height_mm": self.units(3100)}])
        self.assertIn("표 1", out)

    def test_single_row_change_is_refused_because_the_sum_moves(self):
        out = self.fill_fails(make_photo_hwp(section=sized_section()), heights=[{"id": "t0.r0.c0", "height_mm": self.units(3201)}])
        self.assertIn("표 0", out)

    def test_row_touching_a_vertically_merged_cell_is_refused(self):
        out = self.fill_fails(make_photo_hwp(section=sized_section()),
                              heights=[{"id": "t1.r0.c1", "height_mm": self.units(3500)}, {"id": "t1.r1.c1", "height_mm": self.units(2500)}])
        self.assertIn("합쳐진 칸", out)

    def test_bad_heights_input_is_refused(self):
        raw = make_photo_hwp(section=sized_section())
        for heights in ([{"id": "t0.r9.c9", "height_mm": 10}], [{"id": "t0.r0.c0.p0", "height_mm": 10}],
                        [{"id": "t0.r0.c0", "height_mm": 0}], [{"id": "t0.r0.c0", "height_mm": "10"}], [{"id": "t0.r0.c0"}],
                        {"id": "t0.r0.c0"}, [{"id": "t0.r0.c0", "height_mm": 3.0}, {"id": "t0.r0.c1", "height_mm": 4.0}]):
            self.fill_fails(raw, heights=heights)

    def test_same_value_twice_for_a_row_is_fine_but_different_values_are_not(self):
        raw = make_photo_hwp(section=sized_section())
        self.fill_ok(raw, heights=[{"id": "t0.r0.c0", "height_mm": self.units(3200)}, {"id": "t0.r0.c1", "height_mm": self.units(3200)}])
        self.assertEqual(self.heights_of(self.reopen(), ["t0.r0.c0", "t0.r0.c1"]), {"t0.r0.c0": 3200, "t0.r0.c1": 3200})
        self.output().unlink()
        for old in (self.root / "applications" / "회사-직무").glob("*"):
            old.unlink()
        out = self.fill_fails(raw, heights=[{"id": "t0.r0.c0", "height_mm": self.units(3200)}, {"id": "t0.r0.c1", "height_mm": self.units(3300)}])
        self.assertIn("같은 행", out)

    def test_rows_are_equalised_even_when_cells_in_the_row_differed(self):
        raw = make_photo_hwp(section=sized_section())
        before = self.heights_of(hwp.HwpDoc(raw), ["t0.r0.c0", "t0.r0.c1"])
        self.assertEqual(before, {"t0.r0.c0": 3000, "t0.r0.c1": 3200})
        self.fill_ok(raw, heights=[{"id": "t0.r0.c0", "height_mm": self.units(3200)}])
        self.assertEqual(self.heights_of(self.reopen(), ["t0.r0.c0", "t0.r0.c1"]), {"t0.r0.c0": 3200, "t0.r0.c1": 3200})

    def test_heights_work_in_an_uncompressed_document(self):
        raw = make_photo_hwp(compressed=False, section=sized_section())
        self.fill_ok(raw, heights=[{"id": "t0.r0.c0", "height_mm": self.units(3500)}, {"id": "t0.r1.c0", "height_mm": self.units(3700)}])
        self.assertEqual(self.heights_of(self.reopen(), ["t0.r0.c1", "t0.r1.c1"]), {"t0.r0.c1": 3500, "t0.r1.c1": 3700})


class PreviewTest(PhotoBase):
    def preview(self, name="지원서-작성-미리보기.html"):
        return (self.root / "applications" / "회사-직무" / name).read_text(encoding="utf-8")

    def test_preview_is_written_next_to_the_result_with_the_note_first(self):
        out = self.fill_ok(answers=[{"id": "t0.r0.c1", "text": "홍길동"}])
        html = self.preview()
        self.assertIn("미리보기: applications/회사-직무/지원서-작성-미리보기.html", out)
        self.assertIn(hwp.APPROX_NOTE, out)
        self.assertLess(html.index(hwp.APPROX_NOTE), html.index("입사지원서"))
        self.assertTrue(html.index('class="note"') < html.index("<section"))
        self.assertIn("홍길동", html)
        self.assertIn('class="tbl"', html)

    def test_line_spacing_is_read_from_the_paragraph_shape(self):
        extra = para(0, "고정줄간격문단", ps=1)
        self.fill_ok(section=section_body(extra=extra))
        html = self.preview()
        self.assertIn("text-align:center;font-size:10.0pt;line-height:1.2", html)
        self.assertRegex(html, r'line-height:1\.6;[^>]*>고정줄간격문단')

    def test_cell_heights_are_reflected_and_follow_the_redistribution(self):
        raw = make_photo_hwp(section=sized_section())
        self.fill_ok(raw, heights=[{"id": "t0.r0.c0", "height_mm": 3500 / 7200 * 25.4}, {"id": "t0.r1.c0", "height_mm": 3700 / 7200 * 25.4}])
        html = self.preview()
        self.assertIn(f"min-height:{3500 / 7200 * 25.4:.2f}mm", html)
        self.assertIn(f"min-height:{3700 / 7200 * 25.4:.2f}mm", html)
        self.assertNotIn(f"min-height:{3200 / 7200 * 25.4:.2f}mm", html)
        self.assertIn(f"min-height:{5000 / 7200 * 25.4:.2f}mm", html)

    def test_photo_is_embedded_as_base64_at_the_given_size(self):
        data = jpeg_bytes(354, 472)
        self.put_file(data)
        self.fill_ok(answers=[self.photo_item()])
        html = self.preview()
        import base64
        self.assertIn("data:image/jpeg;base64," + base64.b64encode(data).decode(), html)
        self.assertIn("width:30.00mm;height:40.00mm", html)

    def test_png_photo_uses_png_mime(self):
        self.put_file(png_bytes(100, 100), "photos/사진.png")
        self.fill_ok(answers=[self.photo_item(image="photos/사진.png")])
        self.assertIn("data:image/png;base64,", self.preview())

    def test_document_text_is_escaped(self):
        self.fill_ok(answers=[{"id": "t0.r0.c1", "text": "<script>alert(1)</script> & \"x\""}])
        html = self.preview()
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)

    def test_failure_to_render_keeps_the_hwp_and_only_warns(self):
        self.install(make_photo_hwp())
        self.answer([{"id": "t0.r0.c1", "text": "홍길동"}])
        with mock.patch.object(hwp, "render_html", side_effect=RuntimeError("그릴 수 없음")):
            code, out = self.run_cmd("fill")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.output().exists())
        self.assertIn("주의: 미리보기를 만들지 못했습니다(그릴 수 없음)", out)
        self.assertEqual(list((self.root / "applications" / "회사-직무").glob("*.html")), [])

    def test_unreadable_document_styles_only_warn(self):
        self.install(make_hwp())  # DocInfo 가 무작위 바이트라서 그릴 수 없다
        self.answer([{"id": "t0.r0.c1", "text": "홍길동"}])
        code, out = self.run_cmd("fill")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.output().exists())
        self.assertIn("주의: 미리보기를 만들지 못했습니다", out)

    def test_second_run_gets_its_own_pair_of_names(self):
        self.fill_ok(answers=[{"id": "t0.r0.c1", "text": "홍길동"}])
        code, out = self.run_cmd("fill")
        self.assertEqual(code, 0, out)
        names = sorted(p.name for p in (self.root / "applications" / "회사-직무").glob("*"))
        self.assertEqual(names, ["지원서-작성-2-미리보기.html", "지원서-작성-2.hwp", "지원서-작성-미리보기.html", "지원서-작성.hwp"])

    def test_no_preview_when_the_self_check_fails(self):
        self.install(make_photo_hwp())
        self.answer([{"id": "t0.r0.c1", "text": "홍길동"}])
        with mock.patch.object(hwp, "verify", return_value=["4. x"]):
            code, _ = self.run_cmd("fill")
        self.assertEqual(code, 1)
        self.assertEqual(list((self.root / "applications" / "회사-직무").glob("*")), [])


class PhotoSelfCheckTest(PhotoBase):
    def setUp(self):
        super().setUp()
        self.photo = jpeg_bytes(354, 472)
        self.raw = make_photo_hwp(section=sized_section())
        heights = [("t0.r0.c0", 3500 / 7200 * 25.4), ("t0.r1.c0", 3700 / 7200 * 25.4)]
        self.new, _, _, _, self.changed = hwp.fill(self.raw, [("t0.r2.c0", "글", "replace")], heights,
                                                   {"id": "t0.r2.c1", "data": self.photo, "width_mm": 30, "height_mm": 40})

    def messages(self, tampered, changed=None):
        return hwp.verify(self.raw, tampered, changed or self.changed)

    def edit_section(self, edit):
        recs = hwp.parse_records(hwp.inflate(hwp.Cfb(self.new).read("BodyText/Section0")))
        edit(recs)
        return hwp.rebuild_ole(self.new, "BodyText/Section0", hwp.deflate(hwp.build_records(recs)))

    def test_passes_for_a_good_result(self):
        self.assertEqual(self.messages(self.new), [])

    def test_item1_stream_list_must_be_old_plus_the_new_bindata(self):
        self.assertTrue(hwp.verify(make_photo_hwp(extra_streams={"Extra": b"x"}), self.new, self.changed)[0].startswith("1."))
        self.assertTrue(hwp.verify(self.raw, self.raw, self.changed)[0].startswith("1."))

    def test_item2_stream_other_than_docinfo_section_and_new_bindata_changed(self):
        tampered = hwp.rebuild_ole(self.new, "BinData/BIN0001.gif", b"\7" * 5000)
        self.assertTrue(any(m.startswith("2.") and "BIN0001" in m for m in self.messages(tampered)))

    def test_item6_new_bindata_must_inflate_to_the_photo(self):
        other = deflate_raw(jpeg_bytes(354, 472, seed=99))
        tampered = hwp.rebuild_ole(self.new, "BinData/BIN0003.jpg", other)
        self.assertTrue(any(m.startswith("6.") for m in self.messages(tampered)))

    def test_item7_overlapping_sectors_are_found(self):
        cfb = hwp.Cfb(self.new)
        b = bytearray(self.new)
        struct.pack_into("<I", b, cfb.streams["BinData/BIN0003.jpg"]["pos"] + 116, cfb.streams["BinData/BIN0001.gif"]["start"])
        self.assertTrue(any(m.startswith("7.") for m in self.messages(bytes(b))))

    def test_item7_sector_outside_the_file_is_found(self):
        cfb = hwp.Cfb(self.new)
        b = bytearray(self.new)
        struct.pack_into("<I", b, cfb.streams["BinData/BIN0003.jpg"]["pos"] + 116, (len(self.new) - 512) // 512 + 5)
        self.assertTrue(any(m.startswith(("7.", "8.")) for m in self.messages(bytes(b))))

    def test_item8_a_stream_that_cannot_be_read_again_is_found(self):
        cfb = hwp.Cfb(self.new)
        b = bytearray(self.new)
        struct.pack_into("<I", b, cfb.streams["BinData/BIN0001.gif"]["pos"] + 120, 900000)
        self.assertTrue(any(m.startswith("8.") and "BIN0001" in m for m in self.messages(bytes(b))))

    def test_item9_height_other_than_planned(self):
        def edit(recs):
            k = next(i for i, r in enumerate(recs) if r[0] == hwp.LIST_HEADER and struct.unpack_from("<HH", r[2], 8) == (0, 2))
            recs[k][2] = recs[k][2][:20] + struct.pack("<I", 4321) + recs[k][2][24:]
        self.assertTrue(any(m.startswith("9.") for m in self.messages(self.edit_section(edit))))

    def test_item9_byte_other_than_height_in_a_list_header(self):
        def edit(recs):
            k = next(i for i, r in enumerate(recs) if r[0] == hwp.LIST_HEADER and struct.unpack_from("<HH", r[2], 8) == (0, 2))
            recs[k][2] = recs[k][2][:16] + struct.pack("<I", 777) + recs[k][2][20:]
        found = self.messages(self.edit_section(edit))
        self.assertTrue(any(m.startswith("9.") and "높이 외" in m for m in found), found)

    def test_item9_row_height_sum_changed_with_a_matching_plan(self):
        changed = hwp.Changed(self.changed)
        changed.photo = self.changed.photo
        changed.heights = {"cells": [dict(c, new=c["new"] + 10) for c in self.changed.heights["cells"]]}

        def edit(recs):
            cells = [i for i, r in enumerate(recs) if r[0] == hwp.LIST_HEADER and struct.unpack_from("<I", r[2], 4)[0] == 0x20][:4]
            for i in cells:  # 표 0 의 첫 두 행(칸 넷)
                recs[i][2] = recs[i][2][:20] + struct.pack("<I", struct.unpack_from("<I", recs[i][2], 20)[0] + 10) + recs[i][2][24:]
        found = hwp.verify(self.raw, self.edit_section(edit), changed)
        self.assertTrue(any(m.startswith("9.") and "합계" in m for m in found), found)

    def test_item10_picture_number_in_the_cell_differs(self):
        def edit(recs):
            k = next(i for i, r in enumerate(recs) if r[0] == 85)
            recs[k][2] = recs[k][2][:71] + struct.pack("<H", 1)
        self.assertTrue(any(m.startswith("10.") for m in self.messages(self.edit_section(edit))))

    def test_item10_docinfo_changed_beyond_the_new_record(self):
        cfb = hwp.Cfb(self.new)
        recs = hwp.parse_records(hwp.inflate(cfb.read("DocInfo")))
        k = next(i for i, r in enumerate(recs) if r[0] == 21)
        recs[k][2] = recs[k][2][:42] + struct.pack("<i", 2000) + recs[k][2][46:]
        tampered = hwp.rewrite_docinfo(self.new, hwp.build_records(recs), True)
        self.assertTrue(any(m.startswith("10.") for m in self.messages(tampered)))

    def test_item10_docinfo_picture_count_not_raised(self):
        cfb = hwp.Cfb(self.new)
        recs = hwp.parse_records(hwp.inflate(cfb.read("DocInfo")))
        k = next(i for i, r in enumerate(recs) if r[0] == 17)
        recs[k][2] = struct.pack("<I", 2) + recs[k][2][4:]
        tampered = hwp.rewrite_docinfo(self.new, hwp.build_records(recs), True)
        self.assertTrue(any(m.startswith("10.") for m in self.messages(tampered)))

    def test_text_items_still_work_with_a_photo(self):
        self.assertTrue(any(m.startswith("5.") for m in hwp.verify(self.raw, self.new, self._with_wrong_text())))

    def _with_wrong_text(self):
        changed = hwp.Changed({n: {"touched": set(p["touched"]), "expected": {o: t + "!" for o, t in p["expected"].items()}}
                               for n, p in self.changed.items()})
        changed.photo, changed.heights = self.changed.photo, self.changed.heights
        return changed

    def test_failed_item_removes_the_result_and_shows_up_in_the_message(self):
        self.install(self.raw)
        self.put_file(self.photo)
        self.answer_all([self.photo_item("t0.r2.c1")])
        with mock.patch.object(hwp, "verify", return_value=["10. 사진 칸이 다름"]):
            code, out = self.run_cmd("fill")
        self.assertEqual(code, 1)
        self.assertIn("10. 사진 칸이 다름", out)
        self.assertEqual(list((self.root / "applications" / "회사-직무").glob("*")), [])


# ---------- 비정상 입력 ----------

class AbnormalInputTest(PhotoBase):
    def hwp_with_sections(self, sections, **kw):
        return make_hwp(sections=sections, compressed=True, **kw)

    def text_only_fat1(self, filler_count, sections=None):
        header = b"HWP Document File".ljust(32, b"\0") + struct.pack("<II", 0x05000300, 0)
        streams = {"FileHeader": header.ljust(256, b"\0"), "DocInfo": random.Random(3).randbytes(5000),
                   "PrvText": "미리보기".encode("utf-16le")}
        for n, body in enumerate(sections or [section_body(filler_count=filler_count)]):
            streams[f"BodyText/Section{n}"] = body
        return make_cfb(streams, min_fat_sectors=1)

    def test_decompression_bomb_over_the_cap_is_refused_quickly_and_without_much_memory(self):
        import time
        import tracemalloc
        raw = self.hwp_with_sections([b"\0" * (hwp.MAX_SECTION + 1024 * 1024)])
        self.assertLess(len(raw), 1024 * 1024)
        tracemalloc.start()
        started = time.monotonic()
        with self.assertRaises(ft.UserError) as ctx:
            hwp.HwpDoc(raw)
        elapsed = time.monotonic() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertIn("너무 크거나 손상", str(ctx.exception))
        self.assertLess(peak, hwp.MAX_SECTION * 5 // 2)  # 상한이 없던 때는 풀린 크기만큼 계속 늘었다
        self.assertLess(elapsed, 5)

    def test_total_of_all_sections_is_capped(self):
        big = rec(66, 0, b"\0" * (40 * 1024 * 1024))  # 레코드 하나라 레코드 수 상한에는 걸리지 않는다
        self.assertLess(len(big), hwp.MAX_SECTION)
        raw = self.hwp_with_sections([big, big])
        with self.assertRaises(ft.UserError) as ctx:
            hwp.HwpDoc(raw)
        self.assertIn("너무 크거나 손상", str(ctx.exception))

    def test_bomb_is_refused_when_filling_through_the_command(self):
        self.install(self.hwp_with_sections([b"\0" * (hwp.MAX_SECTION + 1024)]))
        self.answer([{"id": "t0.r0.c1", "text": "홍길동"}])
        code, out = self.run_cmd("fill")
        self.assertEqual(code, 1)
        self.assertIn("너무 크거나 손상", out)
        self.assertFalse(self.output().exists())

    def test_record_count_cap(self):
        empty = struct.pack("<I", 0)  # 크기 0 레코드 하나
        self.assertEqual(len(hwp.parse_records(empty * 1000, limit=1000)), 1000)
        with self.assertRaises(ft.UserError) as ctx:
            hwp.parse_records(empty * 1001, limit=1000)
        self.assertIn("너무 크거나 손상", str(ctx.exception))
        self.assertEqual(hwp.MAX_RECORDS, 2_000_000)

    def test_record_count_cap_is_shared_by_all_sections(self):
        section = struct.pack("<I", 0) * 600
        raw = make_hwp(sections=[section, section])
        with mock.patch.object(hwp, "MAX_RECORDS", 1000):
            with self.assertRaises(ft.UserError):
                hwp.HwpDoc(raw)
        with mock.patch.object(hwp, "MAX_RECORDS", 1200):
            self.assertEqual(len(hwp.HwpDoc(raw).sections), 2)

    def test_verify_raising_any_exception_removes_the_output(self):
        for exc in (KeyError("x"), StopIteration(), IndexError("i"), struct.error("s")):
            self.install(make_hwp())
            self.answer([{"id": "t0.r0.c1", "text": "홍길동"}])
            with mock.patch.object(hwp, "verify", side_effect=exc):
                code, out = self.run_cmd("fill")
            self.assertEqual(code, 1)
            self.assertIn("자체 검수 중 오류: " + type(exc).__name__, out)
            self.assertFalse(self.output().exists())
            folder = self.root / "applications" / "회사-직무"
            self.assertEqual(list(folder.glob("*")), [])  # 결과 .hwp 도 미리보기 .html 도 없다

    def test_text_only_fill_grows_the_fat_when_it_has_no_room(self):
        raw = None
        for count in range(40, 400, 10):
            candidate = self.text_only_fat1(count)
            cfb = hwp.Cfb(candidate)
            now = (len(candidate) - 512) // 512
            need = -(-cfb.streams["BodyText/Section0"]["size"] // 512)
            if len(cfb.fat) == 128 and now + need > 128 and now <= 128:
                raw = candidate
                break
        self.assertIsNotNone(raw, "FAT 여유가 모자란 합성 파일을 만들지 못함")
        self.install(raw)
        self.answer([{"id": "t0.r0.c1", "text": "홍길동"}])
        code, out = self.run_cmd("fill")
        self.assertEqual(code, 0, out)
        new = self.output().read_bytes()
        cfb = hwp.Cfb(new)
        self.assertGreater(len(cfb.fat), 128)
        self.assertEqual(len(cfb.difat), 2)
        self.assertEqual(hwp.sector_problems(cfb), [])
        self.assertEqual(self.cell_texts(hwp.HwpDoc(new), "t0.r0.c1"), ["홍길동"])

    def test_grow_fat_refuses_when_the_fat_table_list_is_full(self):
        b, fat, difat = bytearray(512), [hwp.FREESECT] * 128, list(range(109))
        with self.assertRaises(ft.UserError):
            hwp.grow_fat(b, fat, difat, 200, "다시 쓰지")

    def test_big_images_are_refused(self):
        for data in (png_bytes(20001, 100), png_bytes(100, 20001), jpeg_bytes(20001, 100), jpeg_bytes(100, 20001)):
            with self.assertRaises(ft.UserError) as ctx:
                hwp.image_info(data)
            self.assertIn("사진이 너무 큽니다", str(ctx.exception))
        self.assertEqual(hwp.image_info(png_bytes(20000, 20000))[1:], (20000, 20000))

    def test_big_png_is_refused_by_the_fill_command(self):
        self.put_file(png_bytes(70000, 70000), "photos/큰그림.png")
        out = self.fill_fails(make_photo_hwp(), [self.photo_item(image="photos/큰그림.png")])
        self.assertIn("사진이 너무 큽니다", out)
        self.assertNotIn("Traceback", out)

    def test_sector_checks_run_for_text_only_results(self):
        raw = make_hwp(sections=[section_body(filler_count=80), section_body(filler_count=20)])
        new, _, _, _, changed = hwp.fill(raw, [("t0.r0.c1", "홍길동", "replace")])
        self.assertIsNone(getattr(changed, "photo", None))
        self.assertEqual(hwp.verify(raw, new, changed), [])
        with mock.patch.object(hwp, "sector_problems", wraps=hwp.sector_problems) as spy:
            hwp.verify(raw, new, changed)
        spy.assert_called_once()

    def test_text_only_result_sharing_a_sector_fails_item_7(self):
        raw = make_hwp(sections=[section_body(filler_count=80), section_body(filler_count=20)])
        new, _, _, _, changed = hwp.fill(raw, [("t0.r0.c1", "홍길동", "replace")])
        cfb = hwp.Cfb(new)
        b = bytearray(new)
        struct.pack_into("<I", b, cfb.streams["DocInfo"]["pos"] + 116, cfb.streams["BodyText/Section1"]["start"])
        self.assertTrue(any(m.startswith("7.") for m in hwp.verify(raw, bytes(b), changed)))

    def test_text_only_result_with_an_unreadable_stream_fails_item_8(self):
        raw = make_hwp()
        new, _, _, _, changed = hwp.fill(raw, [("t0.r0.c1", "홍길동", "replace")])
        cfb = hwp.Cfb(new)
        b = bytearray(new)
        struct.pack_into("<I", b, cfb.streams["DocInfo"]["pos"] + 120, 99999)  # 사슬보다 큰 크기
        self.assertTrue(any(m.startswith("8.") for m in hwp.verify(raw, bytes(b), changed)))


if __name__ == "__main__":
    unittest.main()
