#!/usr/bin/env python3
"""회사명으로 공공 자료를 조회한다.

  company_lookup.py check  → 키와 연결만 확인한다. 회사명은 보내지 않는다.
  company_lookup.py run    → work/company-query.json 을 읽어 조회하고
                             work/회사/<폴더>/공공자료.md 와 공공자료.json 에 쓴다.
                             후보가 여럿이면 work/company-candidates.md 를 쓰고 멈춘다.

인자는 check / run 하나만 받는다. 키는 환경 변수나 키 파일(~/.config/job-search-kr/.env)에서 이 스크립트만 읽고, 화면과 파일에 쓰지 않는다.
요청마다 먼저 공식 주소인지 검사하고, 인증서를 확인한 연결로만 보내며, 다른 주소로 넘기는 응답은 따르지 않는다.
"""
import html
import io
import json
import os
import re
import socket
import ssl
import sys
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

import _common
from _common import ROOT, WORK, UserError, atomic_write, clean_text, load_json, output_dir, sanitize_name, unique_path

KST = timezone(timedelta(hours=9))
QUERY = WORK / "company-query.json"
CANDIDATES_MD = WORK / "company-candidates.md"
CANDIDATES_JSON = WORK / "company-candidates.json"
CACHE = WORK / "cache"
DART_LIST = CACHE / "dart-corpcode.json"
DART_LIST_DAYS = 30
POSTINGS = ROOT / "postings"

TIMEOUT = 10
MAX_BODY = 5 * 1024 * 1024
MAX_ZIP = 30 * 1024 * 1024
MAX_JOBS = 30 * 1024 * 1024  # 공고가 수백 건인 회사는 본문까지 합쳐 수 MB가 넘는다
MAX_UNZIPPED = 150 * 1024 * 1024
MAX_CALLS = 80
MAX_CANDIDATES = 12

KEY_FILE = Path.home() / ".config" / "job-search-kr" / ".env"
KEY_NAMES = {"data": "DATA_GO_KR_KEY", "dart": "OPENDART_KEY"}

# 요청해도 되는 곳: 주소 이름 → 경로 앞부분. 여기에 없는 주소로는 요청하지 않는다.
OFFICIAL = {
    "apis.data.go.kr": ("/1160100/service/", "/B552015/", "/B490001/"),
    "api.odcloud.kr": ("/api/nts-businessman/v1/",),
    "opendart.fss.or.kr": ("/api/",),
    "boards-api.greenhouse.io": ("/v1/boards/",),
    "api.lever.co": ("/v0/postings/",),
}

FSC_OUTLINE = "https://apis.data.go.kr/1160100/service/GetCorpBasicInfoService_V2/getCorpOutline_V2"
FSC_SUMMARY = "https://apis.data.go.kr/1160100/service/GetFinaStatInfoService_V2/getSummFinaStat_V2"
FSC_BS = "https://apis.data.go.kr/1160100/service/GetFinaStatInfoService_V2/getBs_V2"
NPS = "https://apis.data.go.kr/B552015/NpsBplcInfoInqireServiceV2"
COMWEL = "https://apis.data.go.kr/B490001/gySjbPstateInfoService/getGySjBoheomBsshItem"
NTS_STATUS = "https://api.odcloud.kr/api/nts-businessman/v1/status"
DART = "https://opendart.fss.or.kr/api"
GREENHOUSE = "https://boards-api.greenhouse.io/v1/boards/{}/jobs"
LEVER = "https://api.lever.co/v0/postings/{}"

# 국민연금 보험료율(근로자·사용자 합계). 2025년 연금개혁으로 2026년 9.5%부터 해마다 0.5%p 올라 2033년 13%.
NPS_RATE = {2026: 0.095, 2027: 0.10, 2028: 0.105, 2029: 0.11, 2030: 0.115, 2031: 0.12, 2032: 0.125, 2033: 0.13}
# 기준소득월액 상한. 해당 연도 7월부터 다음 해 6월까지 적용. 국민연금공단 안내가 나오면 한 줄씩 늘린다.
NPS_CAP = {2024: 6_170_000, 2025: 6_370_000, 2026: 6_590_000}
NPS_MIN_MEMBERS = 10

SOURCES = ["오픈다트", "금융위원회 기업기본정보", "금융위원회 재무정보", "국민연금", "근로복지공단", "국세청", "그린하우스", "레버"]

# 결과표에 쓰는 실패 분류
NO_KEY = "키 없음"
KEY_REJECTED = "키 거부"
NOT_APPROVED = "활용신청 승인 안 됨"
LIMIT = "호출 한도 초과"
NO_DATA = "자료 없음"
BLOCKED = "사전 검사에서 막음"
CONN = "연결 실패"
BAD = "응답 오류"


class SourceError(Exception):
    """출처 하나의 조회 실패. 이유에는 주소와 키를 넣지 않는다."""

    def __init__(self, kind, detail=""):
        super().__init__(detail)
        self.kind = kind
        self.detail = detail

    def line(self):
        return self.kind + (f" — {self.detail}" if self.detail else "")


# ---------- 키 ----------

_SECRETS = []


def scrub(text):
    text = str(text)
    for secret in _SECRETS:
        for form in {secret, urllib.parse.quote(secret, safe=""), urllib.parse.quote_plus(secret)}:
            text = text.replace(form, "***")
    return text


def say(text=""):
    print(scrub(text))


def _key_file():
    """키 파일의 이름=값 줄을 사전으로 돌려준다. 파일이 없으면 빈 사전이다."""
    try:
        text = KEY_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            out[name.strip()] = value.strip().strip("\"'")
    return out

def read_key(kind):
    """환경 변수에서, 없으면 키 파일에서 키를 읽는다. (키, 문제). 문제가 있으면 키는 None 이고 문제는 한 줄 이유다."""
    name = KEY_NAMES[kind]
    key = os.environ.get(name)
    if key is None:
        try:
            key = _key_file().get(name)
        except (OSError, UnicodeDecodeError):
            return None, "키 파일을 읽지 못했습니다"
    if key is None:
        return None, "키 파일에 없습니다"
    key = key.strip()  # 복사할 때 딸려 온 앞뒤 탭·공백을 뗀다
    if "%" in key:  # 공공데이터포털 Encoding 키를 넣었으면 Decoding 키로 되돌린다
        key = urllib.parse.unquote(key)
    if not key:
        return None, "키 파일에 빈 값이 들어 있습니다"
    if any(c.isspace() for c in key) or len(key) > 512:
        return None, "키 중간에 공백이 있거나 너무 깁니다"
    if kind == "dart" and not re.fullmatch(r"[0-9a-fA-F]{40}", key):
        return None, f"오픈다트 키는 영문·숫자 40자인데 {len(key)}자입니다"
    _SECRETS.append(key)
    return key, ""

def key_hint(kind):
    # 키 값을 대화에 붙여넣지 않도록, 사용자가 파일을 직접 열어 넣게 안내한다.
    return f"터미널에서 open -e {KEY_FILE} 로 파일을 열고 {KEY_NAMES[kind]}= 뒤에 키를 붙여넣어 저장해 주세요"

# ---------- 요청과 사전 검사 ----------

_CTX = ssl.create_default_context()
assert _CTX.verify_mode == ssl.CERT_REQUIRED and _CTX.check_hostname


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SourceError(BLOCKED, "다른 주소로 넘기라는 응답이 와서 따라가지 않았습니다")


_OPENER = urllib.request.build_opener(_NoRedirect(), urllib.request.HTTPSHandler(context=_CTX))
_calls = 0


def check_url(url):
    """공식 주소 목록의 https 주소인지 본다. 아니면 요청하지 않는다."""
    p = urllib.parse.urlsplit(url)
    if p.scheme != "https":
        raise SourceError(BLOCKED, "https 주소가 아니라 요청하지 않았습니다")
    try:
        port = p.port
    except ValueError:
        port = -1
    if p.username or p.password or port not in (None, 443):
        raise SourceError(BLOCKED, "주소 형식이 공식 주소와 달라 요청하지 않았습니다")
    prefixes = OFFICIAL.get((p.hostname or "").lower())
    if not prefixes or not p.path.startswith(prefixes) or ".." in p.path or "%2e" in p.path.lower():
        raise SourceError(BLOCKED, "공식 주소 목록에 없는 주소라 요청하지 않았습니다")


def read_limited(resp, limit, strict=True):
    chunks, total = [], 0
    while chunk := resp.read(65536):
        total += len(chunk)
        if total > limit:
            if strict:
                raise SourceError(BAD, "응답이 너무 커서 읽지 않았습니다")
            break
        chunks.append(chunk)
    return b"".join(chunks)


def classify_http(code, payload):
    found = error_marker(payload.decode("utf-8", "replace"))
    if found:
        return found
    if code == 401:
        return SourceError(KEY_REJECTED, "등록되지 않았거나 틀린 키입니다. 마이페이지 활용신청 현황에서 이 API가 승인됐는지 확인해 주세요")
    if code == 403:
        return SourceError(NOT_APPROVED, "이 API의 활용신청 승인을 확인해 주세요")
    if code == 404:
        return SourceError(NO_DATA, "찾는 대상이 없습니다(HTTP 404)")
    if code == 429:
        return SourceError(LIMIT, "")
    return SourceError(BAD, f"HTTP {code}")


def fetch(url, data=None, limit=MAX_BODY, timeout=TIMEOUT):
    """검사를 마친 주소로 요청한다. 인증서 확인이 끝난 연결로만 보낸다."""
    headers = {"User-Agent": "job-search-kr/1.0", "Accept": "application/json, application/xml;q=0.9, */*;q=0.5"}
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return read_limited(resp, limit)
    except SourceError:
        raise
    except urllib.error.HTTPError as exc:
        raise classify_http(exc.code, read_limited(exc, 65536, strict=False))
    except ssl.SSLCertVerificationError:
        raise SourceError(BLOCKED, "인증서 확인에 실패해 요청을 보내지 않았습니다")
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise SourceError(BLOCKED, "인증서 확인에 실패해 요청을 보내지 않았습니다")
        if isinstance(exc.reason, (socket.timeout, TimeoutError)):
            raise SourceError(CONN, f"{timeout}초 안에 응답이 없습니다")
        raise SourceError(CONN, "연결하지 못했습니다")
    except (socket.timeout, TimeoutError):
        raise SourceError(CONN, f"{timeout}초 안에 응답이 없습니다")
    except (ssl.SSLError, OSError):
        raise SourceError(CONN, "연결하지 못했습니다")


def request(url, params=None, data=None, limit=MAX_BODY, timeout=TIMEOUT):
    global _calls
    check_url(url)
    full = url + ("?" + urllib.parse.urlencode(params) if params else "")
    check_url(full)
    if _calls >= MAX_CALLS:
        raise SourceError(LIMIT, f"한 번 실행의 호출 상한 {MAX_CALLS}건에 닿았습니다")
    _calls += 1
    return fetch(full, data=data, limit=limit, timeout=timeout)


def tls_check(host):
    """인증서만 확인한다. 요청은 보내지 않는다."""
    try:
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with _CTX.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
    except ssl.SSLCertVerificationError:
        return False, "인증서 확인 실패"
    except (OSError, ssl.SSLError):
        return False, "연결하지 못했습니다"
    issuer = dict(x[0] for x in cert.get("issuer", ())).get("organizationName", "?")
    until = datetime.strptime(cert["notAfter"], "%b %d %H:%M:%S %Y %Z").strftime("%Y-%m-%d")
    return True, f"발급 {issuer}, 만료 {until}"


# ---------- 응답 읽기 ----------

_DG_MARKERS = [
    ("SERVICE_KEY_IS_NOT_REGISTERED", KEY_REJECTED, "이 API에 등록되지 않은 키입니다. 마이페이지 활용신청 현황에서 이 API가 승인됐는지 확인하고, 승인 직후라면 반영될 때까지 기다린 뒤 다시 확인해 주세요"),
    ("SERVICE_ACCESS_DENIED", NOT_APPROVED, "이 API의 활용신청 승인을 확인해 주세요"),
    ("DEADLINE_HAS_EXPIRED", NOT_APPROVED, "활용 기간이 끝났습니다"),
    ("LIMITED_NUMBER_OF_SERVICE_REQUESTS", LIMIT, ""),
    ("UNREGISTERED_IP", BLOCKED, "등록되지 않은 IP입니다"),
]


def error_marker(text):
    for marker, kind, detail in _DG_MARKERS:
        if marker in text:
            return SourceError(kind, detail)
    return None


def safe_xml(text):
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise SourceError(BAD, "DTD 선언이 든 응답이라 읽지 않았습니다")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        raise SourceError(BAD, "XML 형식이 올바르지 않습니다")


def short(text, n=80):
    text = re.sub(r"\s+", " ", clean_text(str(text or ""))).strip()
    return text[:n]


def parse_dg(raw):
    """공공데이터포털 응답에서 항목 목록을 꺼낸다. JSON·XML 둘 다 받는다."""
    text = raw.decode("utf-8-sig", "replace").strip()
    found = error_marker(text)
    if found:
        raise found
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            raise SourceError(BAD, "JSON 형식이 올바르지 않습니다")
        resp = obj.get("response", obj) if isinstance(obj, dict) else {}
        header = resp.get("header") or {}
        body = resp.get("body") or {}
        code, msg = str(header.get("resultCode", "00")), header.get("resultMsg", "")
        items = body.get("items") if isinstance(body, dict) else None
        if isinstance(items, dict):
            items = items.get("item")
    elif text.startswith("<"):
        root = safe_xml(text)
        code = (root.findtext(".//resultCode") or root.findtext(".//returnReasonCode") or "00").strip()
        msg = root.findtext(".//resultMsg") or root.findtext(".//returnAuthMsg") or ""
        items = [{c.tag: (c.text or "").strip() for c in item} for item in root.iter("item")]
    else:
        raise SourceError(BAD, "알아볼 수 없는 응답입니다")
    if code in ("03",) or "NODATA" in str(msg).upper():
        return []
    if code not in ("00", "0", "000"):
        raise SourceError(BAD, f"결과 코드 {short(code, 10)} {short(msg, 60)}")
    if items in (None, "", []):
        return []
    if isinstance(items, dict):
        items = [items]
    return [i for i in items if isinstance(i, dict)]


def parse_dart(raw):
    try:
        obj = json.loads(raw.decode("utf-8-sig", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SourceError(BAD, "오픈다트 응답을 읽지 못했습니다")
    status = str(obj.get("status", ""))
    if status == "000":
        return obj
    if status == "013":
        raise SourceError(NO_DATA, "")
    if status in ("010", "011", "901"):
        raise SourceError(KEY_REJECTED, short(obj.get("message"), 60))
    if status == "020":
        raise SourceError(LIMIT, "")
    if status == "012":
        raise SourceError(BLOCKED, "오픈다트가 이 IP의 접근을 막았습니다")
    if status == "800":
        raise SourceError(CONN, "오픈다트 시스템 점검 중입니다")
    raise SourceError(BAD, f"오픈다트 응답 코드 {short(status, 10)}")


def num(value):
    """'1,234' 같은 글자를 정수로. 없거나 숫자가 아니면 None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    s = str(value).replace(",", "").strip()
    if not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    return int(float(s))


def digits(value):
    return re.sub(r"\D", "", str(value or ""))


# ---------- 이름 ----------

_CORP_WORDS = re.compile(
    r"\(주\)|주식회사|\(유\)|유한회사|유한책임회사|\(재\)|재단법인|\(사\)|사단법인|\(합\)|합자회사|"
    r"\b(?:co\.?,?\s*ltd|inc|corp|corporation|limited|ltd|llc)\b\.?"
)


def norm_name(name):
    s = unicodedata.normalize("NFKC", str(name or "")).lower()
    s = _CORP_WORDS.sub("", s)
    return re.sub(r"[\s.,·\-_()\[\]&/]", "", s)


def region(address):
    parts = str(address or "").split()
    return " ".join(parts[:2])


def ymd(value):
    d = digits(value)
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else (str(value or "").strip() or "")


# ---------- 오픈다트 회사 목록 ----------

def _safe_dir(path):
    if path.is_symlink():
        raise UserError(f"{path.relative_to(ROOT)} 이 심볼릭 링크라서 쓰지 않습니다.")
    path.mkdir(parents=True, exist_ok=True)


def unzip_corp_list(raw):
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        raise SourceError(BAD, "회사 목록 압축 파일이 올바르지 않습니다")
    infos = z.infolist()
    if len(infos) > 5 or sum(i.file_size for i in infos) > MAX_UNZIPPED:
        raise SourceError(BAD, "회사 목록 압축을 풀면 너무 커서 열지 않았습니다")
    if any(i.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED) for i in infos):
        raise SourceError(BAD, "회사 목록에 다른 압축 방식이 들어 있어 열지 않았습니다")
    member = next((i for i in infos if i.filename.lower().endswith(".xml")), None)
    if member is None:
        raise SourceError(BAD, "회사 목록 압축 파일에 XML이 없습니다")
    parts, total = [], 0
    try:
        with z.open(member) as f:
            while chunk := f.read(1 << 20):
                total += len(chunk)
                if total > member.file_size or total > MAX_UNZIPPED:
                    raise SourceError(BAD, "회사 목록 크기가 선언과 달라 열지 않았습니다")
                parts.append(chunk)
    except (zipfile.BadZipFile, zlib.error, EOFError, RuntimeError, NotImplementedError):
        raise SourceError(BAD, "회사 목록 압축 파일이 손상됐습니다")
    root = safe_xml(b"".join(parts).decode("utf-8-sig", "replace"))
    rows = []
    for item in root.iter("list"):
        code = (item.findtext("corp_code") or "").strip()
        name = (item.findtext("corp_name") or "").strip()
        if re.fullmatch(r"\d{8}", code) and name:
            rows.append([code, clean_text(name)[:100], (item.findtext("stock_code") or "").strip(), (item.findtext("modify_date") or "").strip()])
    if not rows:
        raise SourceError(BAD, "회사 목록이 비어 있습니다")
    return rows


def dart_corp_list(key, today):
    if DART_LIST.is_file() and not DART_LIST.is_symlink():
        try:
            saved = json.loads(DART_LIST.read_text(encoding="utf-8"))
            got = datetime.strptime(saved["받은날"], "%Y-%m-%d").date()
            if (today - got).days < DART_LIST_DAYS:
                return saved["회사"]
        except (ValueError, KeyError, json.JSONDecodeError):
            pass
    raw = request(DART + "/corpCode.xml", {"crtfc_key": key}, limit=MAX_ZIP, timeout=60)
    if not raw.startswith(b"PK"):
        text = raw.decode("utf-8", "replace")
        status = re.search(r"<status>(\d+)</status>|\"status\"\s*:\s*\"(\d+)\"", text)
        if status:
            parse_dart(json.dumps({"status": status.group(1) or status.group(2)}).encode())
        raise SourceError(BAD, "회사 목록 대신 알 수 없는 응답이 왔습니다")
    rows = unzip_corp_list(raw)
    _safe_dir(CACHE)
    with atomic_write(DART_LIST) as tmp:
        tmp.write_text(json.dumps({"받은날": today.isoformat(), "회사": rows}, ensure_ascii=False), encoding="utf-8")
    return rows


# ---------- 기록 ----------

class Log:
    """출처마다 성공·실패를 모아 결과표 한 줄로 만든다."""

    def __init__(self):
        self.events = {s: [] for s in SOURCES}

    def ok(self, source, note=""):
        self.events[source].append(("ok", note))

    def fail(self, source, err):
        self.events[source].append(("fail", err))

    def skip(self, source, why):
        self.events[source].append(("skip", why))

    def attempt(self, source, fn, *args, **kwargs):
        try:
            value = fn(*args, **kwargs)
        except SourceError as exc:
            self.fail(source, exc)
            return None
        self.ok(source)
        return value

    def summary(self, source):
        ev = self.events[source]
        if not ev:
            return "조회 안 함"
        fails = [e for k, e in ev if k == "fail"]
        notes = [n for k, n in ev if k == "ok" and n]
        if any(k == "ok" for k, _ in ev):
            extra = [f.line() for f in fails if f.kind != NO_DATA or f.detail] + notes
            return "성공" + (f" (일부: {'; '.join(dict.fromkeys(extra))})" if extra else "")
        if fails and all(f.kind == NO_DATA for f in fails):
            return NO_DATA
        if fails:
            return fails[0].line()
        return "건너뜀 — " + "; ".join(dict.fromkeys(n for k, n in ev if k == "skip"))


# ---------- 회사 찾기 ----------

def cand_from_dart(info, code):
    return {
        "이름": clean_text(info.get("corp_name", "")), "대표자": short(info.get("ceo_nm"), 40),
        "법인등록번호": digits(info.get("jurir_no")), "사업자등록번호": digits(info.get("bizr_no")),
        "오픈다트코드": code, "주소": short(info.get("adres"), 120), "설립일": ymd(info.get("est_dt")),
        "상장": {"Y": "유가증권", "K": "코스닥", "N": "코넥스", "E": "기타(비상장)"}.get(info.get("corp_cls"), ""),
        "홈페이지": short(info.get("hm_url"), 120), "찾은곳": ["오픈다트"], "오픈다트개황": info,
    }


def cand_from_fsc(item):
    market = short(item.get("corpRegMrktDcdNm"), 20)
    return {
        "이름": clean_text(item.get("corpNm", "")), "대표자": short(item.get("enpRprFnm"), 40),
        "법인등록번호": digits(item.get("crno")), "사업자등록번호": digits(item.get("bzno")),
        "오픈다트코드": digits(item.get("fssCorpUnqNo")) if len(digits(item.get("fssCorpUnqNo"))) == 8 else "",
        "주소": short(f"{item.get('enpBsadr') or ''} {item.get('enpDtadr') or ''}", 120), "설립일": ymd(item.get("enpEstbDt")),
        "상장": market, "홈페이지": short(item.get("enpHmpgUrl"), 120), "찾은곳": ["금융위원회"], "금융위개요": item,
    }


def nps_workplace(item):
    return {
        "seq": str(item.get("seq", "")).strip(), "이름": clean_text(item.get("wkplNm", "")),
        "주소": short(item.get("wkplRoadNmDtlAddr"), 120), "앞6자리": digits(item.get("bzowrRgstNo"))[:6],
        "상태": "등록" if str(item.get("wkplJnngStcd")) == "1" else "탈퇴", "자료년월": digits(item.get("dataCrtYm"))[:6],
    }


def same_company(a, b):
    return any(a.get(k) and a.get(k) == b.get(k) for k in ("법인등록번호", "사업자등록번호", "오픈다트코드"))


def merge_into(target, other):
    for k, v in other.items():
        if k == "찾은곳":
            target[k] = list(dict.fromkeys(target.get(k, []) + v))
        elif k == "국민연금사업장":
            seen = {(norm_name(w["이름"]), w["앞6자리"]) for w in target.get(k, [])}
            target[k] = target.get(k, []) + [w for w in v if (norm_name(w["이름"]), w["앞6자리"]) not in seen]
        elif v and not target.get(k):
            target[k] = v


def search_dart(name, key, today, log):
    try:
        rows = dart_corp_list(key, today)
    except SourceError as exc:
        log.fail("오픈다트", exc)
        return []
    target = norm_name(name)
    normed = [(norm_name(r[1]), r) for r in rows]
    exact = [r for n, r in normed if n == target]
    # 비슷한 이름: 찾는 이름이 들어 있거나, 찾는 이름의 대부분을 차지하는 이름
    picked = exact or [r for n, r in normed if len(target) >= 2 and n and (target in n or (n in target and len(n) >= 0.7 * len(target)))]
    # 상장사 먼저, 그다음 최근에 바뀐 회사 먼저
    picked = sorted(picked, key=lambda r: (0 if r[2] else 1, -(num(r[3]) or 0)))[:6]
    out = []
    for code, *_ in picked:
        info = log.attempt("오픈다트", lambda c=code: parse_dart(request(DART + "/company.json", {"crtfc_key": key, "corp_code": c})))
        if info:
            out.append(cand_from_dart(info, code))
    return out


def fsc_outline(key, **query):
    params = {"ServiceKey": key, "pageNo": 1, "numOfRows": 100, "resultType": "json", **query}  # 이름 검색은 부분 일치라 넉넉히 받는다
    return parse_dg(request(FSC_OUTLINE, params))


def search_fsc(name, key, log):
    items = log.attempt("금융위원회 기업기본정보", fsc_outline, key, corpNm=name) or []
    stripped = re.sub(r"\(주\)|㈜|주식회사", "", name).strip()
    if not items and stripped and stripped != name:
        items = log.attempt("금융위원회 기업기본정보", fsc_outline, key, corpNm=stripped) or []
    latest = {}
    for item in items:
        crno = digits(item.get("crno"))
        if crno and (crno not in latest or str(item.get("lastOpegDt", "")) > str(latest[crno].get("lastOpegDt", ""))):
            latest[crno] = item
    return [cand_from_fsc(i) for i in latest.values()]


def search_nps(name, key, log):
    """이름으로 국민연금 사업장을 찾는다. 이름이 들어간 곳을 모두 찾고, 한 사업장이 달마다 한 줄씩 나온다(식별번호도 달마다 다르다).
    찾기만으로는 성공으로 적지 않는다. 맞는 사업장의 자료를 받았을 때 성공으로 적는다."""
    params = {"serviceKey": key, "wkplNm": name, "pageNo": 1, "numOfRows": 100, "dataType": "json"}
    try:
        items = parse_dg(request(NPS + "/getBassInfoSearchV2", params))
    except SourceError as exc:
        log.fail("국민연금", exc)
        return []
    return [nps_workplace(i) for i in items if i.get("seq")]


def group_workplaces(rows):
    """달마다 나온 줄을 사업장 하나로 묶는다(이름과 사업자등록번호 앞 6자리). 최근 달이 앞에 온다."""
    groups = {}
    for w in rows:
        groups.setdefault((norm_name(w["이름"]), w["앞6자리"]), []).append(w)
    out = []
    for (_, prefix), ws in groups.items():
        ws.sort(key=lambda w: w["자료년월"], reverse=True)
        out.append({"이름": ws[0]["이름"], "주소": ws[0]["주소"], "앞6자리": prefix, "상태": ws[0]["상태"],
                    "달별": [{"seq": w["seq"], "자료년월": w["자료년월"]} for w in ws[:12]]})
    return out


def nps_names(names):
    """국민연금에 등록된 이름 표기를 맞히려고 (주)·주식회사 위치를 바꾼 이름도 만든다. 정식명칭이 앞에 온다."""
    out = [short(n, 100) for n in names if short(n, 100)]
    for n in list(out):
        base = re.sub(r"\(주\)|㈜|주식회사", "", n).strip()
        if base:
            out += [f"{base} 주식회사", f"주식회사 {base}", f"(주){base}", f"{base}(주)", base]
    return list(dict.fromkeys(out))


def find_workplaces(names, key, log, prefix="", target="", tries=4):
    """이름을 바꿔 가며 찾다가 맞는 사업장이 나오면 멈춘다. 사업자등록번호 앞 6자리가 있으면 그것으로, 없으면 이름으로 맞춘다."""
    for name in nps_names(names)[:tries]:
        groups = group_workplaces(search_nps(name, key, log))
        mine = [g for g in groups if (g["앞6자리"] == prefix if prefix else norm_name(g["이름"]) == target)]
        if mine:
            return mine
    return []


def find_candidates(name, keys, today, log):
    cands = []
    if keys["dart"]:
        for c in search_dart(name, keys["dart"], today, log):
            match = next((m for m in cands if same_company(m, c)), None)
            merge_into(match, c) if match else cands.append(c)
    if keys["data"]:
        for c in search_fsc(name, keys["data"], log):
            match = next((m for m in cands if same_company(m, c)), None)
            merge_into(match, c) if match else cands.append(c)
        if not cands:  # 공시·금융위원회에 없는 작은 회사는 국민연금에서만 찾는다
            for g in find_workplaces([name], keys["data"], log, target=norm_name(name)):
                cands.append({"이름": g["이름"], "주소": g["주소"], "사업자번호앞6": g["앞6자리"], "찾은곳": ["국민연금"], "국민연금사업장": [g]})
    target = norm_name(name)
    # 이름이 정확히 같은 회사 먼저, 그중 공시하는 회사 먼저
    cands.sort(key=lambda c: (norm_name(c.get("이름")) != target, "오픈다트" not in c.get("찾은곳", [])))
    return cands[:MAX_CANDIDATES], len(cands)


def write_candidates(name, cands, today, total):
    more = f" 찾은 {total}곳 중 {len(cands)}곳만 적었습니다. 없으면 정식 회사명이나 사업자등록번호로 다시 찾습니다." if total > len(cands) else ""
    lines = [
        f"# {md(name)} 후보", f"조회일: {today}", "",
        "이름이 같은 회사가 여럿이거나 비슷한 이름만 찾았습니다. 사용자가 번호를 고르면 요청 파일의 후보번호에 넣고 다시 실행합니다." + more, "",
        "| 번호 | 이름 | 대표자 | 소재지 | 설립일 | 상장 | 찾은 곳 |", "|---|---|---|---|---|---|---|",
    ]
    for i, c in enumerate(cands, 1):
        lines.append(f"| {i} | {md(c.get('이름'))} | {md(c.get('대표자'))} | {md(region(c.get('주소')))} | {md(c.get('설립일'))} | {md(c.get('상장'))} | {md(', '.join(c.get('찾은곳', [])))} |")
    _safe_dir(WORK)
    with atomic_write(CANDIDATES_MD) as tmp:
        tmp.write_text(scrub("\n".join(lines) + "\n"), encoding="utf-8")
    slim = [{k: v for k, v in c.items() if k not in ("오픈다트개황", "금융위개요")} for c in cands]
    with atomic_write(CANDIDATES_JSON) as tmp:
        tmp.write_text(scrub(json.dumps({"회사명": name, "조회일": str(today), "후보": slim}, ensure_ascii=False, indent=1)), encoding="utf-8")


def pick_saved(name, number):
    if CANDIDATES_JSON.is_symlink() or not CANDIDATES_JSON.is_file():
        raise UserError("후보 목록이 없습니다. 요청 파일의 후보번호를 비우고 다시 실행해 주세요.")
    saved = load_json(CANDIDATES_JSON)
    if saved.get("회사명") != name:
        raise UserError("후보 목록이 다른 회사의 것입니다. 후보번호를 비우고 다시 실행해 주세요.")
    cands = saved.get("후보") or []
    if not 1 <= number <= len(cands):
        raise UserError(f"후보번호는 1부터 {len(cands)} 사이여야 합니다.")
    return cands[number - 1]


def resolve(q, keys, today, log):
    """조회할 회사 하나를 정한다. (회사, "ok") / (None, "candidates") / (None, "none")"""
    if q["후보번호"]:
        return pick_saved(q["회사명"], q["후보번호"]), "ok"
    cands, total = find_candidates(q["회사명"], keys, today, log)
    if q["사업자등록번호"]:
        match = [c for c in cands if c.get("사업자등록번호") == q["사업자등록번호"]]
        if match:
            return match[0], "ok"
        return {"이름": q["회사명"], "사업자등록번호": q["사업자등록번호"], "찾은곳": ["요청 파일"]}, "ok"
    exact = [c for c in cands if norm_name(c.get("이름")) == norm_name(q["회사명"])]
    if len(exact) == 1:
        return exact[0], "ok"
    if not cands:
        return None, "none"
    write_candidates(q["회사명"], cands, today, total)
    return None, "candidates"


# ---------- 회사 자료 모으기 ----------

def dart_year_rows(key, code, api, today, label):
    """최근 사업보고서가 있는 연도를 찾아 목록을 돌려준다(작년, 재작년 순)."""
    for year in (today.year - 1, today.year - 2):
        try:
            obj = parse_dart(request(f"{DART}/{api}.json", {"crtfc_key": key, "corp_code": code, "bsns_year": year, "reprt_code": "11011"}))
            return year, obj.get("list") or []
        except SourceError as exc:
            if exc.kind != NO_DATA:
                raise
    raise SourceError(NO_DATA, f"{label} 없음(최근 2개 연도 사업보고서가 없는 회사)")


ACCOUNTS = {
    "매출": ("매출액", "수익(매출액)", "영업수익", "매출"),
    "영업이익": ("영업이익", "영업이익(손실)"),
    "순이익": ("당기순이익", "당기순이익(손실)", "연결당기순이익"),
    "자산": ("자산총계",),
    "부채": ("부채총계",),
    "자본": ("자본총계",),
    "유동자산": ("유동자산",),
    "유동부채": ("유동부채",),
}


def dart_financials(rows, year):
    cfs = [r for r in rows if r.get("fs_div") == "CFS"]
    use, basis = (cfs, "연결") if cfs else ([r for r in rows if r.get("fs_div") == "OFS"], "개별")
    years = [{"연도": year - i} for i in range(3)]
    for field, names in ACCOUNTS.items():
        row = next((r for n in names for r in use if str(r.get("account_nm", "")).strip() == n), None)
        if row:
            for i, col in enumerate(("thstrm_amount", "frmtrm_amount", "bfefrmtrm_amount")):
                years[i][field] = num(row.get(col))
    years = [y for y in years if any(v is not None for k, v in y.items() if k != "연도")]
    return {"기준": basis, "출처": f"오픈다트 {year} 사업보고서 주요 계정", "연도별": years} if years else None


def fsc_financials(key, crno, today, log):
    """한 해씩 거슬러 올라가며 조회해 자료가 있는 최근 3개 연도를 모은다.
    연도 없이 조회하면 오래된 해부터 오고, 비상장사는 자료가 몇 해 늦게 올라오기도 한다."""
    params = {"serviceKey": key, "pageNo": 1, "numOfRows": 10, "resultType": "json", "crno": crno}
    items, found = [], 0
    for year in range(today.year - 1, today.year - 6, -1):
        try:
            got = parse_dg(request(FSC_SUMMARY, {**params, "bizYear": year}))
        except SourceError as exc:
            log.fail("금융위원회 재무정보", exc)
            return None
        items += got
        found += bool(got)
        if found == 3:
            break
    by_year = {}
    for it in items:
        y = num(it.get("bizYear"))
        if y is None:
            continue
        linked = "연결" in str(it.get("fnclDcdNm", ""))
        if y not in by_year or (linked and not by_year[y][0]):
            by_year[y] = (linked, it)
    if not by_year:
        log.fail("금융위원회 재무정보", SourceError(NO_DATA, "이 회사 재무가 없음(사업보고서를 내지 않는 회사는 대개 없다)"))
        return None
    log.ok("금융위원회 재무정보")
    years = []
    for y in sorted(by_year, reverse=True)[:3]:
        it = by_year[y][1]
        years.append({"연도": y, "매출": num(it.get("enpSaleAmt")), "영업이익": num(it.get("enpBzopPft")), "순이익": num(it.get("enpCrtmNpf")),
                      "자산": num(it.get("enpTastAmt")), "부채": num(it.get("enpTdbtAmt")), "자본": num(it.get("enpTcptAmt"))})
    latest = years[0]["연도"]
    bs = log.attempt("금융위원회 재무정보", lambda: parse_dg(request(FSC_BS, {**params, "numOfRows": 300, "bizYear": latest}))) or []
    linked = by_year[latest][0]
    for field, name in (("유동자산", "유동자산"), ("유동부채", "유동부채")):
        row = next((b for b in bs if str(b.get("acitNm", "")).strip() == name and ("연결" in str(b.get("fnclDcdNm", ""))) == linked), None)
        if row:
            for i, col in enumerate(("crtmAcitAmt", "pvtrAcitAmt", "bpvtrAcitAmt")):
                if i < len(years):
                    years[i][field] = num(row.get(col))
    return {"기준": "연결" if linked else "개별", "출처": f"금융위원회 기업 재무정보 {latest}년", "연도별": years}


def indicators(fin):
    """다섯 지표. 값을 낼 수 없으면 이유를 적는다."""
    ys = fin["연도별"]
    cur = ys[0]
    out = {}

    def ratio(a, b, name, need_positive_b=True):
        if a is None or b is None:
            out[name] = (None, "값 없음")
        elif need_positive_b and b <= 0:
            out[name] = (None, "나누는 값이 0 이하")
        else:
            out[name] = (a / b, "")

    if len(ys) >= 3 and ys[2].get("매출") and cur.get("매출") and ys[2]["매출"] > 0 and cur["매출"] > 0:
        span = cur["연도"] - ys[2]["연도"]
        out["3년 매출 증가율(연평균)"] = ((cur["매출"] / ys[2]["매출"]) ** (1 / span) - 1, f"{ys[2]['연도']}→{cur['연도']}")
    else:
        out["3년 매출 증가율(연평균)"] = (None, "3개 연도 매출이 없거나 0 이하")
    ratio(cur.get("영업이익"), cur.get("매출"), "영업이익률")
    ratio(cur.get("부채"), cur.get("자본"), "부채비율")
    ratio(cur.get("유동자산"), cur.get("유동부채"), "유동비율")
    ratio(cur.get("순이익"), cur.get("자본"), "자기자본이익률(ROE)")
    return out


def nps_rate(year):
    if year < min(NPS_RATE):
        return 0.09
    return NPS_RATE.get(year, max(NPS_RATE.values()))


def nps_cap(year, month):
    period = year if month >= 7 else year - 1
    known = [y for y in NPS_CAP if y <= period]
    return NPS_CAP[max(known)] if known else min(NPS_CAP.values())


def salary_estimate(notice, members, yyyymm):
    """당월 고지금액 ÷ 가입자 수 ÷ 보험료율. 추정이다."""
    if not notice or not members:
        return None, "고지금액이나 가입자 수가 없음"
    if members < NPS_MIN_MEMBERS:
        return None, f"가입자 {members}명이라 추정하지 않음({NPS_MIN_MEMBERS}명 미만)"
    year, month = int(yyyymm[:4]), int(yyyymm[4:6])
    monthly = notice / members / nps_rate(year)
    note = f"추정 · {yyyymm[:4]}년 보험료율 {nps_rate(year) * 100:.1f}% 적용"
    if monthly >= nps_cap(year, month) * 0.9:
        note += " · 상한에 걸려 실제는 더 높을 수 있음"
    return {"월": round(monthly), "연": round(monthly) * 12, "기준월": yyyymm}, note


def nps_detail(key, seq):
    items = parse_dg(request(NPS + "/getDetailInfoSearchV2", {"serviceKey": key, "seq": seq, "dataType": "json"}))
    if not items:
        raise SourceError(NO_DATA, "")
    return items[0]


def nps_period(key, seq, ym):
    items = parse_dg(request(NPS + "/getPdAcctoSttusInfoSearchV2", {"serviceKey": key, "seq": seq, "dataCrtYm": ym, "dataType": "json"}))
    if not items:
        raise SourceError(NO_DATA, "")
    return items[0]


def collect_nps(key, groups, log):
    """사업장마다 최근 달의 가입자·고지금액을 받고, 가입자가 가장 많은 사업장은 12개월의 가입자·입사·퇴사를 받는다.
    달마다 식별번호가 달라 그 달의 식별번호로 그 달 자료를 조회한다."""
    live = [g for g in groups if g.get("상태") == "등록"][:5] or groups[:1]
    details = []
    for g in live:
        latest = g["달별"][0]
        it = log.attempt("국민연금", nps_detail, key, latest["seq"])
        if it:
            details.append({"이름": g["이름"], "주소": g["주소"], "자료년월": latest["자료년월"], "가입자수": num(it.get("jnngpCnt")),
                            "고지금액": num(it.get("crrmmNtcAmt")), "등록일": ymd(it.get("adptDt")),
                            "업종": short(it.get("vldtVlKrnNm"), 60), "달별": g["달별"]})
    if not details:
        return None
    main = max(details, key=lambda d: d["가입자수"] or 0)
    monthly = []
    for i, m in enumerate(main["달별"]):
        members = main["가입자수"] if i == 0 else num((log.attempt("국민연금", nps_detail, key, m["seq"]) or {}).get("jnngpCnt"))
        period = log.attempt("국민연금", nps_period, key, m["seq"], m["자료년월"]) or {}
        monthly.append({"년월": m["자료년월"], "가입자": members, "입사": num(period.get("nwAcqzrCnt")), "퇴사": num(period.get("lssJnngpCnt"))})
    monthly.sort(key=lambda m: m["년월"])
    total_members = sum(d["가입자수"] or 0 for d in details)
    total_notice = sum(d["고지금액"] or 0 for d in details)
    estimate, note = salary_estimate(total_notice, total_members, main["자료년월"] or "")
    for d in details:
        d.pop("달별", None)
    return {"사업장": details, "가입자합계": total_members, "월별": monthly, "월별기준사업장": main["이름"],
            "평균월급추정": estimate, "추정메모": note}


def collect_comwel(key, bzno):
    for flag, label in (("2", "고용"), ("1", "산재")):
        items = parse_dg(request(COMWEL, {"serviceKey": key, "v_saeopjaDrno": bzno, "opaBoheomFg": flag, "pageNo": 1, "numOfRows": 10}))
        if items:
            return [{"보험": label, "사업장명": short(i.get("saeopjangNm"), 60), "상시인원": num(i.get("sangsiInwonCnt")),
                     "성립일": ymd(i.get("seongripDt")), "업종": short(i.get("gyEopjongNm") or i.get("sjEopjongNm"), 60),
                     "주소": short(i.get("addr"), 120)} for i in items]
    raise SourceError(NO_DATA, "")


def collect_nts(key, bzno):
    raw = request(NTS_STATUS, {"serviceKey": key, "returnType": "JSON"}, data={"b_no": [bzno]})
    found = error_marker(raw.decode("utf-8", "replace"))
    if found:
        raise found
    obj = load_json_response(raw, "국세청")
    if not isinstance(obj, dict):
        raise SourceError(BAD, "국세청 응답 형식이 다릅니다")
    if obj.get("status_code") != "OK":
        raise SourceError(BAD, short(obj.get("msg") or obj.get("status_code"), 60))
    row = (obj.get("data") or [{}])[0]
    return {"상태": short(row.get("b_stt"), 20) or "국세청에 등록되지 않은 번호", "과세유형": short(row.get("tax_type"), 60),
            "폐업일": ymd(row.get("end_dt"))}


# ---------- 그린하우스·레버 ----------

class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "table"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


KOREA = re.compile(r"korea|seoul|서울|한국|대한민국|판교|pangyo", re.I)


def html_to_text(value):
    s = str(value or "")
    if "&lt;" in s:
        s = html.unescape(s)
    parser = _Text()
    parser.feed(s)
    text = clean_text("".join(parser.out))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def load_json_response(raw, what):
    try:
        return json.loads(raw.decode("utf-8-sig", "replace"))
    except json.JSONDecodeError:
        raise SourceError(BAD, f"{what} 응답을 읽지 못했습니다")


def greenhouse_jobs(board):
    obj = load_json_response(request(GREENHOUSE.format(board), {"content": "true"}, limit=MAX_JOBS, timeout=30), "그린하우스")
    if not isinstance(obj, dict):
        raise SourceError(BAD, "그린하우스 응답 형식이 다릅니다")
    jobs = []
    for j in obj.get("jobs") or []:
        jobs.append({"출처": "그린하우스", "ID": str(j.get("id", "")), "직무": short(j.get("title"), 120),
                     "근무지": short((j.get("location") or {}).get("name"), 80),
                     "부서": short(", ".join(d.get("name", "") for d in j.get("departments") or []), 80),
                     "게시일": str(j.get("first_published") or j.get("updated_at") or ""),
                     "게시일항목": "first_published" if j.get("first_published") else "updated_at",
                     "주소": str(j.get("absolute_url") or ""), "본문": html_to_text(j.get("content"))})
    return jobs


def lever_jobs(site):
    obj = load_json_response(request(LEVER.format(site), {"mode": "json"}, limit=MAX_JOBS, timeout=30), "레버")
    jobs = []
    for j in obj if isinstance(obj, list) else []:
        cat = j.get("categories") or {}
        created = j.get("createdAt")
        when = datetime.fromtimestamp(created / 1000, KST).strftime("%Y-%m-%d") if isinstance(created, (int, float)) else ""
        body = [html_to_text(j.get("description") or j.get("descriptionPlain"))]
        for part in j.get("lists") or []:
            body.append(f"{short(part.get('text'), 120)}\n{html_to_text(part.get('content'))}")
        body.append(html_to_text(j.get("additional") or j.get("additionalPlain")))
        jobs.append({"출처": "레버", "ID": str(j.get("id", "")), "직무": short(j.get("text"), 120),
                     "근무지": short(cat.get("location"), 80), "부서": short(cat.get("team") or cat.get("department"), 80),
                     "게시일": when, "게시일항목": "createdAt", "주소": str(j.get("hostedUrl") or ""),
                     "본문": "\n\n".join(b for b in body if b)})
    return jobs


def save_posting(company, job, today):
    if POSTINGS.is_symlink():
        raise UserError("postings 폴더가 심볼릭 링크라서 쓰지 않습니다.")
    POSTINGS.mkdir(exist_ok=True)
    stem = f"{today}-{sanitize_name(company, '회사', 40)}-{sanitize_name(job['직무'], '직무', 60)}"
    path = unique_path(POSTINGS, stem, ".md")
    lines = [
        f"게시일({job['출처']} {job['게시일항목']} 값): {job['게시일'] or '조회한 곳에 없음'}",
        f"출처: {job['주소']}", f"조회일: {today}", "", f"# {job['직무']}",
        f"- 근무지: {job['근무지'] or '-'}", f"- 부서: {job['부서'] or '-'}", "", job["본문"], "",
    ]
    with atomic_write(path) as tmp:
        tmp.write_text(scrub("\n".join(lines)), encoding="utf-8")
    return path


# ---------- 결과 파일 ----------

def md(value):
    return re.sub(r"\s+", " ", clean_text(str(value if value is not None else ""))).replace("|", "／").strip()


def won(v):
    if v is None:
        return "-"
    if abs(v) >= 1e12:
        return f"{v / 1e12:,.1f}조"
    return f"{v / 1e8:,.0f}억" if abs(v) >= 1e8 else f"{v:,}원"


def manwon(v):
    return "-" if v is None else f"{v / 1e4:,.0f}만원"


def pct(v):
    return "-" if v is None else f"{v * 100:.1f}%"


def build_markdown(r):
    ident = r["식별"]
    out = [f"# {md(ident.get('이름'))} 공공 자료", f"조회일: {r['조회일']}", "",
           "이 파일은 조회 스크립트가 쓴다. 값은 출처와 기준일을 달아 회사분석.md 로 옮긴다.", "", "## 조회 상태", "",
           "| 출처 | 결과 |", "|---|---|"]
    out += [f"| {s} | {md(v)} |" for s, v in r["조회 상태"].items()]
    if any(v.startswith(NO_KEY) for v in r["조회 상태"].values()):
        out += ["", "키가 없는 출처는 키를 키 파일에 넣은 뒤 `company_lookup.py check` 로 확인하고 다시 조회한다."]
    out += ["", "## 식별", ""]
    for label in ("이름", "대표자", "법인등록번호", "사업자등록번호", "오픈다트코드", "설립일", "주소", "홈페이지", "상장", "업종", "중소기업", "감사의견"):
        if ident.get(label):
            out.append(f"- {label}: {md(ident[label])}")
    out += ["", "## 사업자 상태", ""]
    nts = r.get("사업자 상태")
    out.append(f"- 국세청: {md(nts['상태'])}" + (f", {md(nts['과세유형'])}" if nts.get("과세유형") else "") + (f", 폐업일 {nts['폐업일']}" if nts.get("폐업일") else "") if nts else "- 국세청: 조회하지 못함(조회 상태 참고)")
    out += ["", "## 직원", ""]
    staff = r["직원"]
    if staff.get("오픈다트"):
        d = staff["오픈다트"]
        out += [f"오픈다트 {d['연도']} 사업보고서 직원 현황(합계 {d['합계']:,}명)" if d.get("합계") else f"오픈다트 {d['연도']} 사업보고서 직원 현황", "",
                "| 사업부문 | 성별 | 정규직 | 계약직 | 합계 | 평균 근속 | 1인 평균 급여 |", "|---|---|---|---|---|---|---|"]
        for row in d["행"]:
            out.append("| " + " | ".join(md(row.get(k)) for k in ("사업부문", "성별", "정규직", "계약직", "합계", "평균근속", "평균급여")) + " |")
        out.append("")
    if staff.get("금융위원회"):
        f = staff["금융위원회"]
        count = num(f.get("종업원수"))
        out.append(f"- 금융위원회 기업개요: 종업원 {'-' if count is None else format(count, ',')}명, 평균 근속 {md(f.get('평균근속') or '-')}년, 1인 평균 급여 {manwon(num(f.get('평균급여')))}")
    for c in staff.get("근로복지공단") or []:
        out.append(f"- 근로복지공단({c['보험']}보험): 상시 인원 {'-' if c['상시인원'] is None else format(c['상시인원'], ',')}명, 성립일 {c['성립일'] or '-'}, 업종 {md(c['업종'])}, {md(c['사업장명'])}(이 사업자등록번호로 등록된 사업장만)")
    nps = staff.get("국민연금")
    if nps:
        out.append(f"- 국민연금: 사업장 {len(nps['사업장'])}곳, 가입자 합계 {nps['가입자합계']:,}명")
        for w in nps["사업장"]:
            out.append(f"  - {md(w['이름'])} ({md(region(w['주소']))}): 가입자 {'-' if w['가입자수'] is None else format(w['가입자수'], ',')}명, 자료 {w['자료년월'] or '-'}, 등록일 {w['등록일'] or '-'}")
        est = nps["평균월급추정"]
        out.append(f"- 국민연금 기준 평균 월급: 약 {est['월'] / 1e4:,.0f}만원(연 {est['연'] / 1e4:,.0f}만원) — {md(nps['추정메모'])}" if est else f"- 국민연금 기준 평균 월급: {md(nps['추정메모'])}")
        if nps["월별"]:
            dash = lambda v: "-" if v is None else f"{v:,}"
            out += ["", f"국민연금 월별 가입자·입사·퇴사({md(nps['월별기준사업장'])})", "", "| 년월 | 가입자 | 입사 | 퇴사 |", "|---|---|---|---|"]
            out += [f"| {m['년월'][:4]}-{m['년월'][4:]} | {dash(m['가입자'])} | {dash(m['입사'])} | {dash(m['퇴사'])} |" for m in nps["월별"]]
    if not any(staff.values()):
        out.append("- 조회한 자료 없음")
    out += ["", "## 재무", ""]
    fin = r.get("재무")
    if fin:
        out += [f"{md(fin['출처'])} · {fin['기준']} 기준", "", "| 연도 | 매출 | 영업이익 | 순이익 | 자산 | 부채 | 자본 | 유동자산 | 유동부채 |", "|---|---|---|---|---|---|---|---|---|"]
        for y in fin["연도별"]:
            out.append(f"| {y['연도']} | " + " | ".join(won(y.get(k)) for k in ("매출", "영업이익", "순이익", "자산", "부채", "자본", "유동자산", "유동부채")) + " |")
        out += ["", "| 지표 | 값 | 메모 |", "|---|---|---|"]
        out += [f"| {k} | {pct(v)} | {md(note)} |" for k, (v, note) in r["지표"].items()]
        out += ["", "업종 평균과 판정(O·△·X)은 기업 분석 단계에서 찾아 붙인다."]
    else:
        out.append("- 재무 자료 없음(조회 상태 참고)")
    if r.get("공고"):
        korea = sum(1 for j in r["공고"] if KOREA.search(j["근무지"]))
        out += ["", "## 열린 공고", "", f"열린 공고 {len(r['공고'])}건, 그중 한국 근무 {korea}건. 한국 근무를 먼저 적었다.", "",
                "| ID | 직무 | 부서 | 근무지 | 게시일 | 출처 |", "|---|---|---|---|---|---|"]
        out += [f"| {md(j['ID'])} | {md(j['직무'])} | {md(j['부서'])} | {md(j['근무지'])} | {md(j['게시일'][:10])} | {j['출처']} |" for j in r["공고"][:60]]
        if len(r["공고"]) > 60:
            out.append(f"\n공고 {len(r['공고'])}건 중 60건만 적었다. 나머지는 공공자료.json 에 있다.")
    if r.get("저장한 공고"):
        out += ["", f"저장한 공고: {r['저장한 공고']}"]
    out += ["", "## 출처", ""] + [f"- {s}" for s in r["출처"]]
    return "\n".join(out) + "\n"


# ---------- 실행 ----------

def load_query():
    data = load_json(QUERY)
    if not isinstance(data, dict):
        raise UserError("work/company-query.json 은 {\"회사명\": ..., \"폴더\": ...} 형식이어야 합니다.")
    name = short(data.get("회사명"), 100)
    if not name:
        raise UserError("요청 파일에 회사명이 없습니다.")
    if not isinstance(data.get("폴더"), str) or not data["폴더"].strip():
        raise UserError("요청 파일에 폴더(<회사>-<직무>)가 없습니다.")
    bzno = digits(data.get("사업자등록번호"))
    if bzno and len(bzno) != 10:
        raise UserError("사업자등록번호는 숫자 10자리여야 합니다.")
    pick = data.get("후보번호")
    if pick in ("", None):
        pick = None
    elif not (isinstance(pick, int) or (isinstance(pick, str) and pick.isdigit())):
        raise UserError("후보번호는 숫자여야 합니다.")
    q = {"회사명": name, "폴더": data["폴더"], "사업자등록번호": bzno, "후보번호": int(pick) if pick else None}
    for field in ("그린하우스", "레버", "저장할공고"):
        value = str(data.get(field) or "").strip()
        if value and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
            raise UserError(f"{field} 값은 영문·숫자·하이픈만 씁니다(채용 페이지 주소의 해당 부분).")
        q[field] = value
    return q


def run():
    q = load_query()
    today = datetime.now(KST).date()
    read = {kind: read_key(kind) for kind in KEY_NAMES}
    keys = {kind: key for kind, (key, _) in read.items()}
    log = Log()
    for kind, sources in (("dart", ["오픈다트"]), ("data", ["금융위원회 기업기본정보", "금융위원회 재무정보", "국민연금", "근로복지공단", "국세청"])):
        if not keys[kind]:
            for s in sources:
                log.fail(s, SourceError(NO_KEY, read[kind][1]))

    fallback = {"이름": q["회사명"], "사업자등록번호": q["사업자등록번호"], "찾은곳": []}
    company, state = fallback, "ok"
    if keys["dart"] or keys["data"]:
        company, state = resolve(q, keys, today, log)
    if state == "none" and (q["그린하우스"] or q["레버"]):
        company, state = fallback, "ok"  # 국내 법인을 못 찾아도 외국계 공고는 조회한다
        for s in SOURCES[:6]:
            if not log.events[s]:
                log.skip(s, "국내 법인을 이름으로 찾지 못함")
    if state == "candidates":
        say(f"후보가 여럿이라 멈췄습니다: {CANDIDATES_MD.relative_to(ROOT)}")
        say("사용자가 번호를 고르면 요청 파일의 후보번호에 넣고 다시 실행하세요.")
        return 3
    if state == "none":
        say(f"'{q['회사명']}' 을(를) 찾지 못했습니다. 공고의 정식 회사명이나 사업자등록번호를 요청 파일에 넣고 다시 실행하세요.")
        for s in SOURCES[:6]:
            say(f"  {s}: {log.summary(s)}")
        return 4

    sources_used = []
    ident = {k: company.get(k, "") for k in ("이름", "대표자", "법인등록번호", "사업자등록번호", "오픈다트코드", "설립일", "주소", "홈페이지", "상장")}
    staff, fin, nts = {}, None, None
    crno, bzno, dart_code = ident["법인등록번호"], ident["사업자등록번호"] or q["사업자등록번호"], ident["오픈다트코드"]

    if keys["dart"] and dart_code:
        info = company.get("오픈다트개황") or log.attempt("오픈다트", lambda: parse_dart(request(DART + "/company.json", {"crtfc_key": keys["dart"], "corp_code": dart_code})))
        if info:
            merge_into(ident, {k: v for k, v in cand_from_dart(info, dart_code).items() if k in ident})
            ident["업종"] = ident.get("업종") or f"업종코드 {short(info.get('induty_code'), 10)}"
            crno, bzno = crno or ident["법인등록번호"], bzno or ident["사업자등록번호"]
            sources_used.append("오픈다트 기업개황 API")
        emp = log.attempt("오픈다트", dart_year_rows, keys["dart"], dart_code, "empSttus", today, "직원 현황")
        if emp and emp[1]:
            year, rows = emp
            totals = [x for x in rows if "합계" in str(x.get("fo_bbm", ""))] or rows  # 성별합계 행이 있으면 그것만 더한다
            staff["오픈다트"] = {"연도": year, "합계": sum(num(x.get("sm")) or 0 for x in totals), "행": [
                {"사업부문": x.get("fo_bbm"), "성별": x.get("sexdstn"), "정규직": x.get("rgllbr_co"), "계약직": x.get("cnttk_co"),
                 "합계": x.get("sm"), "평균근속": x.get("avrg_cnwk_sdytrn"), "평균급여": manwon(num(x.get("jan_salary_am")))} for x in rows[:20]]}
            sources_used.append(f"오픈다트 직원 현황 API, {year} 사업보고서")
        acc = log.attempt("오픈다트", dart_year_rows, keys["dart"], dart_code, "fnlttSinglAcnt", today, "재무 주요 계정")
        if acc and acc[1]:
            fin = dart_financials(acc[1], acc[0])
            if fin:
                sources_used.append(f"오픈다트 단일회사 주요계정 API, {acc[0]} 사업보고서")
    elif keys["dart"] and not log.events["오픈다트"]:
        log.skip("오픈다트", "오픈다트에 공시 회사로 없음")

    if keys["data"]:
        outline = company.get("금융위개요")
        if not outline and crno:
            items = log.attempt("금융위원회 기업기본정보", fsc_outline, keys["data"], crno=crno)
            outline = items[0] if items else None
        if outline:
            # 이 API는 값이 없을 때 0을 준다. 0은 없는 값으로 본다
            staff["금융위원회"] = {k: v for k, v in (("종업원수", outline.get("enpEmpeCnt")), ("평균근속", outline.get("empeAvgCnwkTermCtt")),
                                                   ("평균급여", outline.get("enpPn1AvgSlryAmt"))) if (num(v) or 0) > 0} or None
            ident["업종"] = short(outline.get("sicNm"), 60) or ident.get("업종", "")
            ident["중소기업"] = {"Y": "예", "N": "아니요"}.get(outline.get("smenpYn"), "")
            ident["감사의견"] = short(outline.get("audtRptOpnnCtt"), 40)
            merge_into(ident, {k: v for k, v in cand_from_fsc(outline).items() if k in ident})
            crno, bzno = crno or ident["법인등록번호"], bzno or ident["사업자등록번호"]
            sources_used.append("금융위원회 기업기본정보 기업개요조회 API")
        elif not crno:
            log.skip("금융위원회 기업기본정보", "법인등록번호를 찾지 못함")

        if not fin and crno:
            fin = fsc_financials(keys["data"], crno, today, log)
            if fin:
                sources_used.append(f"금융위원회 기업 재무정보 API({fin['출처']})")
        elif fin:
            log.skip("금융위원회 재무정보", "오픈다트 재무를 씀")
        else:
            log.skip("금융위원회 재무정보", "법인등록번호를 찾지 못함")

        workplaces = company.get("국민연금사업장") or []
        if not workplaces and bzno:
            names = [ident.get("이름"), (company.get("금융위개요") or {}).get("corpNm"), q["회사명"]]
            workplaces = find_workplaces(names, keys["data"], log, prefix=bzno[:6])
        if workplaces:
            staff["국민연금"] = collect_nps(keys["data"], workplaces, log)
            if staff["국민연금"]:
                sources_used.append(f"국민연금공단 국민연금 가입 사업장 내역 API(자료 {staff['국민연금']['사업장'][0]['자료년월']})")
        else:
            log.skip("국민연금", "사업장을 찾지 못함")

        if bzno:
            staff["근로복지공단"] = log.attempt("근로복지공단", collect_comwel, keys["data"], bzno)
            if staff["근로복지공단"]:
                sources_used.append("근로복지공단 고용·산재보험 현황정보 API")
            nts = log.attempt("국세청", collect_nts, keys["data"], bzno)
            if nts:
                sources_used.append("국세청 사업자등록 상태조회 API")
        else:
            log.skip("근로복지공단", "사업자등록번호를 찾지 못함")
            log.skip("국세청", "사업자등록번호를 찾지 못함")

    jobs = []
    for field, source, fn in (("그린하우스", "그린하우스", greenhouse_jobs), ("레버", "레버", lever_jobs)):
        if q[field]:
            found = log.attempt(source, fn, q[field])
            if found is not None:
                jobs += found
                sources_used.append(f"{source} 공개 채용 API({q[field]})")
    jobs.sort(key=lambda j: (0 if KOREA.search(j["근무지"]) else 1, j["직무"]))
    saved = None
    if q["저장할공고"]:
        job = next((j for j in jobs if j["ID"] == q["저장할공고"]), None)
        if job is None:
            raise UserError(f"저장할공고 {q['저장할공고']} 를 열린 공고에서 찾지 못했습니다.")
        saved = save_posting(ident.get("이름") or q["회사명"], job, today).relative_to(ROOT)

    result = {
        "조회일": str(today), "요청": {"회사명": q["회사명"], "폴더": q["폴더"]}, "식별": ident,
        "조회 상태": {s: log.summary(s) for s in SOURCES if s not in ("그린하우스", "레버") or q[s]},
        "사업자 상태": nts, "직원": {k: v for k, v in staff.items() if v}, "재무": fin,
        "지표": indicators(fin) if fin else {}, "공고": jobs, "저장한 공고": str(saved) if saved else "",
        "출처": list(dict.fromkeys(sources_used)),
    }
    target = output_dir(q["폴더"], WORK / "회사")
    md_path, json_path = target / "공공자료.md", target / "공공자료.json"
    with atomic_write(md_path) as tmp:
        tmp.write_text(scrub(build_markdown(result)), encoding="utf-8")
    serial = {**result, "지표": {k: {"값": v, "메모": n} for k, (v, n) in result["지표"].items()}}
    with atomic_write(json_path) as tmp:
        tmp.write_text(scrub(json.dumps(serial, ensure_ascii=False, indent=1)), encoding="utf-8")
    say(f"썼습니다: {md_path.relative_to(ROOT)}")
    for s, line in result["조회 상태"].items():
        say(f"  {s}: {line}")
    for kind in KEY_NAMES:
        if not keys[kind]:
            say(key_hint(kind))
    if saved:
        say(f"공고 원문: {saved}")
    return 0


def check():
    say("인증서")
    for host in OFFICIAL:
        ok, detail = tls_check(host)
        say(f"  {'통과' if ok else '실패'} {host} — {detail}")
    read = {kind: read_key(kind) for kind in KEY_NAMES}
    keys = {kind: key for kind, (key, _) in read.items()}
    tests = [
        ("금융위원회 기업기본정보", "data", lambda k: parse_dg(request(FSC_OUTLINE, {"ServiceKey": k, "pageNo": 1, "numOfRows": 1, "resultType": "json"}))),
        ("금융위원회 재무정보", "data", lambda k: parse_dg(request(FSC_SUMMARY, {"serviceKey": k, "pageNo": 1, "numOfRows": 1, "resultType": "json"}))),
        ("국민연금", "data", lambda k: parse_dg(request(NPS + "/getBassInfoSearchV2", {"serviceKey": k, "wkplNm": "국민연금공단", "pageNo": 1, "numOfRows": 1, "dataType": "json"}))),
        ("근로복지공단", "data", lambda k: parse_dg(request(COMWEL, {"serviceKey": k, "pageNo": 1, "numOfRows": 1, "opaBoheomFg": "2"}))),
        ("국세청", "data", lambda k: collect_nts(k, "0000000000")),
        ("오픈다트", "dart", lambda k: parse_dart(request(DART + "/company.json", {"crtfc_key": k, "corp_code": "00126380"}))),
    ]
    say("키")
    failed = False
    for source, kind, fn in tests:
        if not keys[kind]:
            say(f"  {source}: {NO_KEY} — {read[kind][1]}. {key_hint(kind)}")
            failed = True
            continue
        try:
            fn(keys[kind])
            say(f"  {source}: 통과")
        except SourceError as exc:
            if exc.kind == NO_DATA:
                say(f"  {source}: 통과(자료 없음 응답)")
            else:
                say(f"  {source}: {exc.line()}")
                failed = True
    return 1 if failed else 0


def main(argv):
    if len(argv) != 2 or argv[1] not in ("check", "run"):
        print("사용법: company_lookup.py check | run")
        return 2
    return check() if argv[1] == "check" else run()


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except UserError as exc:
        say(f"오류: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # 예상 못 한 오류도 키를 가린 한 줄로만 보인다
        say(f"오류: 예상하지 못한 문제입니다({type(exc).__name__}: {exc})")
        sys.exit(1)
