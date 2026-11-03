#!/usr/bin/env python3
"""work/doc.md 와 work/doc.json 으로 Word(.docx) 파일을 만든다.

외부 패키지 없이 파이썬 기본 기능만 쓴다. 인자를 받지 않는다.
결과: applications/<폴더>/<파일명>.docx (같은 이름이 있으면 번호를 붙인다)
"""
import re
import sys
import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

from _common import WORK, ROOT, UserError, atomic_write, clean_text, load_json, output_dir, sanitize_name, unique_path

PLACEHOLDERS = ("[수치 확인 필요]", "[확인 필요]")
FONTS = {"ko": ("맑은 고딕", "맑은 고딕"), "en": ("Calibri", "맑은 고딕")}
PAGE_W, PAGE_H, MARGIN = 11906, 16838, 1134  # A4, 여백 20mm (twip)
TEXT_W = PAGE_W - 2 * MARGIN
SEPARATOR_ROW = re.compile(r"^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?$")

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def runs(text, bold=False, size=None):
    """**굵게** 표시를 나눠 w:r 들을 만든다."""
    out = []
    for i, part in enumerate(text.split("**")):
        if not part:
            continue
        props = []
        if bold or i % 2 == 1:
            props.append("<w:b/>")
        if size:
            props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
        rpr = f"<w:rPr>{''.join(props)}</w:rPr>" if props else ""
        out.append(f'<w:r>{rpr}<w:t xml:space="preserve">{escape(part)}</w:t></w:r>')
    return "".join(out)


def paragraph(text, style=None, align=None, indent=None, bold=False, size=None, keep_next=False, page_break=False):
    ppr = []
    if style:
        ppr.append(f'<w:pStyle w:val="{style}"/>')
    if keep_next:
        ppr.append("<w:keepNext/>")
    if page_break:
        ppr.append("<w:pageBreakBefore/>")
    if indent:
        left, hanging = indent
        ppr.append(f'<w:ind w:left="{left}" w:hanging="{hanging}"/>')
    if align:
        ppr.append(f'<w:jc w:val="{align}"/>')
    ppr_xml = f"<w:pPr>{''.join(ppr)}</w:pPr>" if ppr else ""
    return f"<w:p>{ppr_xml}{runs(text, bold=bold, size=size)}</w:p>"


def table(rows):
    ncols = max(len(r) for r in rows)
    col_w = TEXT_W // ncols
    border = '<w:{0} w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
    borders = "".join(border.format(side) for side in ("top", "left", "bottom", "right", "insideH", "insideV"))
    parts = [
        "<w:tbl><w:tblPr>",
        f'<w:tblW w:w="{col_w * ncols}" w:type="dxa"/>',
        '<w:tblLayout w:type="fixed"/>',
        f"<w:tblBorders>{borders}</w:tblBorders>",
        '<w:tblCellMar><w:left w:w="100" w:type="dxa"/><w:right w:w="100" w:type="dxa"/></w:tblCellMar>',
        "</w:tblPr><w:tblGrid>",
        "".join(f'<w:gridCol w:w="{col_w}"/>' for _ in range(ncols)),
        "</w:tblGrid>",
    ]
    for r_index, row in enumerate(rows):
        header = r_index == 0
        parts.append("<w:tr>")
        cells = row + [""] * (ncols - len(row))
        for cell in cells:
            shade = '<w:shd w:val="clear" w:color="auto" w:fill="E7E6E6"/>' if header else ""
            parts.append(f'<w:tc><w:tcPr><w:tcW w:w="{col_w}" w:type="dxa"/>{shade}</w:tcPr>')
            lines = cell.split("<br>") if cell else [""]
            for line in lines:
                parts.append(paragraph(line.strip(), style="TableText", bold=header))
            parts.append("</w:tc>")
        parts.append("</w:tr>")
    parts.append("</w:tbl>")
    # 표 바로 뒤에 빈 문단이 있어야 Word가 다음 표와 붙이지 않는다
    parts.append('<w:p><w:pPr><w:pStyle w:val="TableText"/></w:pPr></w:p>')
    return "".join(parts)


def split_row(line):
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def convert(markdown):
    body = []
    table_rows = []
    # 쪽 나눔은 다음 문단의 "앞에서 쪽 나눔" 속성으로 건다. 빈 문단에 나눔 문자를 넣으면 미리보기에서 네모로 보인다.
    pending_break = False

    def flush_table():
        if table_rows:
            body.append(table(table_rows))
            table_rows.clear()

    def add(text, **kwargs):
        nonlocal pending_break
        body.append(paragraph(text, page_break=pending_break, **kwargs))
        pending_break = False

    for raw in markdown.split("\n"):
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("|"):
            if not SEPARATOR_ROW.match(stripped):
                if pending_break and not table_rows:
                    # 표에는 문단 속성을 걸 수 없어 나눔 문자를 쓴다
                    body.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
                    pending_break = False
                table_rows.append(split_row(stripped))
            continue
        flush_table()
        if not stripped:
            continue
        if stripped == "---":
            pending_break = bool(body)
        elif stripped.startswith("### "):
            add(stripped[4:], style="Heading3", keep_next=True)
        elif stripped.startswith("## "):
            add(stripped[3:], style="Heading2", keep_next=True)
        elif stripped.startswith("# "):
            add(stripped[2:], style="Title", align="center")
        elif re.match(r"^[-*] ", stripped):
            depth = (len(line) - len(line.lstrip(" "))) // 2
            left = 360 + 360 * min(depth, 3)
            add("• " + stripped[2:], indent=(left, 240))
        else:
            add(stripped)
    flush_table()
    return "".join(body)


def document_xml(body):
    sect = (
        f'<w:sectPr><w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}"/>'
        f'<w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" w:bottom="{MARGIN}" w:left="{MARGIN}" '
        'w:header="567" w:footer="567" w:gutter="0"/></w:sectPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{body}{sect}</w:body></w:document>'
    )


def styles_xml(lang):
    latin, east = FONTS.get(lang, FONTS["ko"])
    fonts = f'<w:rFonts w:ascii="{latin}" w:hAnsi="{latin}" w:eastAsia="{east}" w:cs="{latin}"/>'

    def style(style_id, name, size, bold=True, space_before=0, space_after=80, border=False):
        b = "<w:b/><w:bCs/>" if bold else ""
        pbdr = '<w:pBdr><w:bottom w:val="single" w:sz="6" w:space="1" w:color="808080"/></w:pBdr>' if border else ""
        return (
            f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>'
            '<w:basedOn w:val="Normal"/><w:next w:val="Normal"/><w:qFormat/>'
            f'<w:pPr>{pbdr}<w:spacing w:before="{space_before}" w:after="{space_after}"/></w:pPr>'
            f'<w:rPr>{b}<w:sz w:val="{size}"/><w:szCs w:val="{size}"/></w:rPr></w:style>'
        )

    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:styles xmlns:w="{W_NS}">'
        f'<w:docDefaults><w:rPrDefault><w:rPr>{fonts}<w:sz w:val="21"/><w:szCs w:val="21"/>'
        f'<w:lang w:val="{"ko-KR" if lang == "ko" else "en-US"}" w:eastAsia="ko-KR"/></w:rPr></w:rPrDefault>'
        '<w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
        "</w:docDefaults>"
        '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/></w:style>'
        + style("Title", "Title", 36, space_after=240)
        + style("Heading2", "heading 2", 26, space_before=240, space_after=120, border=True)
        + style("Heading3", "heading 3", 22, space_before=160, space_after=60)
        + style("TableText", "Table Text", 20, bold=False, space_after=0)
        + "</w:styles>"
    )


CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    '<Override PartName="/docProps/app.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
    "</Types>"
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
    'Target="docProps/core.xml"/>'
    '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" '
    'Target="docProps/app.xml"/>'
    "</Relationships>"
)
DOC_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/>'
    "</Relationships>"
)


def core_xml(title):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{escape(title)}</dc:title>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{now}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{now}</dcterms:modified>'
        "</cp:coreProperties>"
    )


# 프로그램 이름은 비워 둔다. 파일 속성에서 회사가 볼 수 있다.
APP_XML = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"/>'
)


def main():
    md_path = WORK / "doc.md"
    if md_path.is_symlink() or not md_path.is_file():
        raise UserError('work/doc.md 파일이 없습니다. work/doc.md 본문과 work/doc.json {"filename", "folder", "lang"}이 필요합니다.')
    if md_path.stat().st_size > 2_000_000:
        raise UserError("work/doc.md 파일이 너무 큽니다.")
    markdown = clean_text(md_path.read_text(encoding="utf-8").replace("\r\n", "\n"))
    left = [p for p in PLACEHOLDERS if p in markdown]
    if left:
        raise UserError(f"확인이 필요한 표시가 남아 있어 만들지 않습니다: {', '.join(left)}")

    meta = load_json(WORK / "doc.json")
    if not isinstance(meta, dict):
        raise UserError("work/doc.json 은 객체여야 합니다.")
    lang = meta.get("lang", "ko")
    if lang not in FONTS:
        raise UserError("lang 은 ko 또는 en 이어야 합니다.")
    filename = sanitize_name(meta.get("filename"), "문서")
    target_dir = output_dir(meta.get("folder"))
    out = unique_path(target_dir, filename, ".docx")

    with atomic_write(out) as tmp, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("word/document.xml", document_xml(convert(markdown)))
        z.writestr("word/styles.xml", styles_xml(lang))
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("docProps/core.xml", core_xml(filename))
        z.writestr("docProps/app.xml", APP_XML)
    print(f"만들었습니다: {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except UserError as exc:
        print(f"오류: {exc}")
        sys.exit(1)
