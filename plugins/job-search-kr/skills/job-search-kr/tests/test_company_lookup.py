"""조회 스크립트 시험. 네트워크 없이 돈다(인증서 시험만 이 컴퓨터 안의 가짜 서버를 쓴다).

    python3 -m unittest discover -s tests -v
"""
import contextlib
import io
import json
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import _common  # noqa: E402
import company_lookup as cl  # noqa: E402

DART_KEY = "0123456789abcdef0123456789abcdef01234567"
DATA_KEY = "Zx9+Test/Key==Q1w2e3r4"


def corp_zip(rows):
    xml = "<result>" + "".join(
        f"<list><corp_code>{c}</corp_code><corp_name>{n}</corp_name><stock_code>{s}</stock_code><modify_date>20260101</modify_date></list>"
        for c, n, s in rows) + "</result>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("CORPCODE.xml", xml)
    return buf.getvalue()


def dg_json(items):
    return json.dumps({"response": {"header": {"resultCode": "00", "resultMsg": "NORMAL SERVICE."},
                                    "body": {"items": {"item": items}, "totalCount": len(items)}}}).encode()


def fin_row(name, cur, prev, prev2):
    return {"fs_div": "CFS", "account_nm": name, "thstrm_amount": f"{cur:,}", "frmtrm_amount": f"{prev:,}", "bfefrmtrm_amount": f"{prev2:,}"}


class FakeNet:
    """주소별로 정해 둔 응답을 돌려준다. 받은 요청을 모두 기록한다."""

    def __init__(self, corp_rows):
        self.calls = []
        self.corp_rows = corp_rows

    def __call__(self, url, data=None, limit=None, timeout=None):
        self.calls.append((url, data))
        p = urllib.parse.urlsplit(url)
        q = dict(urllib.parse.parse_qsl(p.query))
        path = p.path
        if path == "/api/corpCode.xml":
            return corp_zip(self.corp_rows)
        if path == "/api/company.json":
            code = q["corp_code"]
            info = {"00000001": ("가나다(주)", "1101110000001", "1234567890", "E"),
                    "00000002": ("가나다", "1101110000002", "2234567890", "E")}.get(code, ("가나다(주)", "1101110000001", "1234567890", "E"))
            return json.dumps({"status": "000", "corp_name": info[0], "ceo_nm": "홍길동", "jurir_no": info[1], "bizr_no": info[2],
                               "adres": "서울특별시 강남구 테헤란로 1", "est_dt": "20100101", "corp_cls": info[3], "induty_code": "620"}).encode()
        if path == "/api/empSttus.json":
            return json.dumps({"status": "013", "message": "조회된 데이타가 없습니다."}).encode()
        if path == "/api/fnlttSinglAcnt.json":
            if q["bsns_year"] != "2025":
                return json.dumps({"status": "013"}).encode()
            rows = [fin_row("매출액", 1210, 1100, 1000), fin_row("영업이익", 121, 100, 90), fin_row("당기순이익", 50, 40, 30),
                    fin_row("자산총계", 3000, 2800, 2600), fin_row("부채총계", 1000, 900, 800), fin_row("자본총계", 2000, 1900, 1800),
                    fin_row("유동자산", 1500, 1400, 1300), fin_row("유동부채", 750, 700, 650)]
            return json.dumps({"status": "000", "list": rows}).encode()
        if path.endswith("/getCorpOutline_V2"):
            return dg_json([{"crno": "1101110000001", "bzno": "1234567890", "corpNm": "가나다(주)", "enpEmpeCnt": "120",
                             "empeAvgCnwkTermCtt": "4.2", "enpPn1AvgSlryAmt": "55000000", "sicNm": "소프트웨어 개발", "smenpYn": "Y",
                             "audtRptOpnnCtt": "적정", "fssCorpUnqNo": "00000001", "lastOpegDt": "20260901"}])
        if path.endswith("/getBassInfoSearchV2"):
            # 이름이 들어간 곳을 모두 찾는다. 한 사업장이 달마다 식별번호가 다른 줄로 나온다
            if "가나다" not in q["wkplNm"]:
                return dg_json([])
            row = {"wkplNm": "가나다 주식회사", "bzowrRgstNo": "123456****", "wkplJnngStcd": "1", "wkplRoadNmDtlAddr": "서울특별시 강남구 테헤란로 1"}
            other = {"seq": "900", "wkplNm": "하청/일용/가나다 공사", "bzowrRgstNo": "999999****", "wkplJnngStcd": "1", "dataCrtYm": "202607"}
            return dg_json([{**row, "seq": "111", "dataCrtYm": "202607"}, {**row, "seq": "110", "dataCrtYm": "202606"}, other])
        if path.endswith("/getDetailInfoSearchV2"):
            # 가입자 100명, 고지금액 = 100명 × 400만원 × 9.5%
            members = {"111": 100, "110": 98}[q["seq"]]
            return dg_json([{"jnngpCnt": str(members), "crrmmNtcAmt": str(members * 4_000_000 * 95 // 1000), "adptDt": "20100301", "vldtVlKrnNm": "응용 소프트웨어 개발"}])
        if path.endswith("/getPdAcctoSttusInfoSearchV2"):
            # 그 달의 식별번호로 조회해야만 그 달 자료가 나온다
            data = {("111", "202607"): ("3", "2"), ("110", "202606"): ("1", "0")}.get((q["seq"], q["dataCrtYm"]))
            return dg_json([{"nwAcqzrCnt": data[0], "lssJnngpCnt": data[1]}] if data else [])
        if path.endswith("/getGySjBoheomBsshItem"):
            return ("<response><header><resultCode>00</resultCode><resultMsg>NORMAL SERVICE</resultMsg></header><body><items><item>"
                    "<saeopjangNm>가나다(주)</saeopjangNm><sangsiInwonCnt>118</sangsiInwonCnt><seongripDt>20100301</seongripDt>"
                    "<gyEopjongNm>소프트웨어 개발</gyEopjongNm><addr>서울 강남구</addr></item></items></body></response>").encode()
        if path.endswith("/status"):
            return json.dumps({"status_code": "OK", "data": [{"b_no": "1234567890", "b_stt": "계속사업자", "tax_type": "부가가치세 일반과세자", "end_dt": ""}]}).encode()
        raise AssertionError(f"예상하지 않은 요청: {path}")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        work = self.tmp / "work"
        work.mkdir()
        patches = {
            "ROOT": self.tmp, "WORK": work, "QUERY": work / "company-query.json",
            "CANDIDATES_MD": work / "company-candidates.md", "CANDIDATES_JSON": work / "company-candidates.json",
            "CACHE": work / "cache", "DART_LIST": work / "cache" / "dart-corpcode.json", "POSTINGS": self.tmp / "postings",
            "KEY_FILE": self.tmp / ".env",
        }
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(mock.patch.multiple(cl, **patches))
        self.stack.enter_context(mock.patch.multiple(_common, ROOT=self.tmp, APPLICATIONS=self.tmp / "applications"))
        self.stack.enter_context(mock.patch.object(cl, "_calls", 0))
        self.stack.enter_context(mock.patch.dict(cl.os.environ))
        for name in cl.KEY_NAMES.values():
            cl.os.environ.pop(name, None)
        cl._SECRETS.clear()

    def tearDown(self):
        self.stack.close()
        shutil.rmtree(self.tmp)

    def keys(self, data=DATA_KEY, dart=DART_KEY):
        values = {cl.KEY_NAMES["data"]: data, cl.KEY_NAMES["dart"]: dart}
        return mock.patch.dict(cl.os.environ, {k: v for k, v in values.items() if v is not None})

    def key_file(self, text):
        cl.KEY_FILE.write_text(text, encoding="utf-8")

    def run_query(self, query, net):
        cl.QUERY.write_text(json.dumps(query, ensure_ascii=False), encoding="utf-8")
        out = io.StringIO()
        with self.keys(), mock.patch.object(cl, "fetch", net), contextlib.redirect_stdout(out):
            code = cl.run()
        return code, out.getvalue()


class PrecheckTest(Base):
    def test_official_addresses_pass(self):
        for url in (cl.FSC_OUTLINE, cl.NPS + "/getBassInfoSearchV2", cl.COMWEL, cl.NTS_STATUS, cl.DART + "/company.json",
                    cl.GREENHOUSE.format("airbnb"), cl.LEVER.format("palantir")):
            cl.check_url(url)

    def test_blocked_addresses(self):
        for url in ("http://apis.data.go.kr/B552015/x", "https://evil.example.com/api/", "https://apis.data.go.kr/other/",
                    "https://user:pw@opendart.fss.or.kr/api/x", "https://opendart.fss.or.kr:8443/api/x",
                    "https://opendart.fss.or.kr/api/../admin", "https://opendart.fss.or.kr/api/%2e%2e/admin",
                    "https://opendart.fss.or.kr.evil.com/api/x"):
            with self.assertRaises(cl.SourceError, msg=url) as ctx:
                cl.check_url(url)
            self.assertEqual(ctx.exception.kind, cl.BLOCKED)

    def test_blocked_address_is_never_fetched(self):
        net = mock.Mock()
        with mock.patch.object(cl, "fetch", net):
            with self.assertRaises(cl.SourceError):
                cl.request("https://evil.example.com/api/x", {"serviceKey": DATA_KEY})
        net.assert_not_called()

    def test_redirect_is_refused(self):
        with self.assertRaises(cl.SourceError) as ctx:
            cl._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example.com/")
        self.assertEqual(ctx.exception.kind, cl.BLOCKED)

    def test_call_limit(self):
        with mock.patch.object(cl, "fetch", return_value=b"{}"), mock.patch.object(cl, "_calls", cl.MAX_CALLS):
            with self.assertRaises(cl.SourceError) as ctx:
                cl.request(cl.DART + "/company.json")
        self.assertEqual(ctx.exception.kind, cl.LIMIT)


class CertificateTest(unittest.TestCase):
    """스스로 서명한 인증서를 쓰는 가짜 서버에는 요청 내용이 한 줄도 가지 않아야 한다."""

    def test_untrusted_certificate_sends_nothing(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1", "-days", "1",
                            "-keyout", str(tmp / "k.pem"), "-out", str(tmp / "c.pem")], check=True, capture_output=True)
            received = []

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    received.append(self.path)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"{}")

                def log_message(self, *a):
                    pass

            server = HTTPServer(("127.0.0.1", 0), Handler)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(tmp / "c.pem", tmp / "k.pem")
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
            server.handle_error = lambda *a: None
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with self.assertRaises(cl.SourceError) as err:
                    cl.fetch(f"https://127.0.0.1:{server.server_port}/api/company.json?crtfc_key={DART_KEY}", timeout=5)
                self.assertEqual(err.exception.kind, cl.BLOCKED)
                self.assertIn("인증서", err.exception.detail)
                self.assertEqual(received, [])
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(tmp)


class KeyTest(Base):
    def test_reads_key_file(self):
        self.key_file(f"# 주석\nDATA_GO_KR_KEY={DATA_KEY}\nOPENDART_KEY=\t{DART_KEY} \n")
        self.assertEqual(cl.read_key("data"), (DATA_KEY, ""))
        self.assertEqual(cl.read_key("dart"), (DART_KEY, ""))

    def test_quoted_value_in_key_file(self):
        self.key_file(f'OPENDART_KEY="{DART_KEY}"\n')
        self.assertEqual(cl.read_key("dart"), (DART_KEY, ""))

    def test_environment_wins_over_key_file(self):
        self.key_file("OPENDART_KEY=" + "f" * 40 + "\n")
        with self.keys():
            self.assertEqual(cl.read_key("dart"), (DART_KEY, ""))

    def test_missing_file_and_missing_name(self):
        key, problem = cl.read_key("dart")
        self.assertIsNone(key)
        self.assertIn("없습니다", problem)
        self.key_file(f"DATA_GO_KR_KEY={DATA_KEY}\n")
        key, problem = cl.read_key("dart")
        self.assertIsNone(key)
        self.assertIn("없습니다", problem)

    def test_empty_and_wrong_length(self):
        self.key_file("DATA_GO_KR_KEY=\nOPENDART_KEY=abc\n")
        key, problem = cl.read_key("data")
        self.assertIsNone(key)
        self.assertIn("빈 값", problem)
        key, problem = cl.read_key("dart")
        self.assertIsNone(key)
        self.assertIn("40자", problem)

    def test_encoding_key_is_decoded(self):
        self.key_file("DATA_GO_KR_KEY=" + urllib.parse.quote(DATA_KEY, safe="") + "\n")
        self.assertEqual(cl.read_key("data"), (DATA_KEY, ""))

    def test_scrub_hides_key_in_every_form(self):
        cl._SECRETS.append(DATA_KEY)
        text = f"{DATA_KEY} {urllib.parse.quote(DATA_KEY, safe='')} {urllib.parse.quote_plus(DATA_KEY)}"
        self.assertNotIn("Zx9", cl.scrub(text))


class ParseTest(unittest.TestCase):
    def test_data_go_kr_errors(self):
        xml = b"<OpenAPI_ServiceResponse><cmmMsgHeader><errMsg>SERVICE ERROR</errMsg><returnAuthMsg>SERVICE_KEY_IS_NOT_REGISTERED_ERROR</returnAuthMsg><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>"
        with self.assertRaises(cl.SourceError) as ctx:
            cl.parse_dg(xml)
        self.assertEqual(ctx.exception.kind, cl.KEY_REJECTED)
        self.assertEqual(cl.classify_http(403, b"Forbidden").kind, cl.NOT_APPROVED)
        self.assertEqual(cl.classify_http(401, b"Unauthorized").kind, cl.KEY_REJECTED)

    def test_items_shapes(self):
        self.assertEqual(cl.parse_dg(dg_json({"a": "1"})), [{"a": "1"}])
        self.assertEqual(cl.parse_dg(json.dumps({"response": {"header": {"resultCode": "00"}, "body": {"items": ""}}}).encode()), [])
        self.assertEqual(cl.parse_dg(b"<response><header><resultCode>00</resultCode></header><body><items><item><x>1</x></item></items></body></response>"), [{"x": "1"}])
        with self.assertRaises(cl.SourceError):
            cl.parse_dg(b'<!DOCTYPE x [<!ENTITY a "b">]><response/>')

    def test_dart_status(self):
        self.assertEqual(cl.parse_dart(b'{"status":"000"}')["status"], "000")
        for status, kind in (("013", cl.NO_DATA), ("010", cl.KEY_REJECTED), ("020", cl.LIMIT)):
            with self.assertRaises(cl.SourceError) as ctx:
                cl.parse_dart(json.dumps({"status": status}).encode())
            self.assertEqual(ctx.exception.kind, kind)

    def test_corp_list_zip_checks(self):
        self.assertEqual(cl.unzip_corp_list(corp_zip([("00000001", "가나다", "")]))[0][:2], ["00000001", "가나다"])
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_BZIP2) as z:
            z.writestr("CORPCODE.xml", "<result/>")
        with self.assertRaises(cl.SourceError):
            cl.unzip_corp_list(buf.getvalue())

    def test_html_to_text(self):
        text = cl.html_to_text("&lt;p&gt;Hello&lt;/p&gt;&lt;ul&gt;&lt;li&gt;A&lt;/li&gt;&lt;/ul&gt;&lt;script&gt;x()&lt;/script&gt;")
        self.assertEqual(text, "Hello\n\n- A")

    def test_names(self):
        self.assertEqual(cl.norm_name("(주)가나다"), cl.norm_name("가나다 주식회사"))
        self.assertEqual(cl.norm_name("㈜가나다"), cl.norm_name("가나다"))
        self.assertEqual(cl.norm_name("Princeton Inc."), "princeton")


class CalcTest(unittest.TestCase):
    def test_indicators(self):
        fin = {"연도별": [{"연도": 2025, "매출": 1210, "영업이익": 121, "순이익": 50, "부채": 1000, "자본": 2000, "유동자산": 1500, "유동부채": 750},
                          {"연도": 2024, "매출": 1100}, {"연도": 2023, "매출": 1000}]}
        out = cl.indicators(fin)
        self.assertAlmostEqual(out["3년 매출 증가율(연평균)"][0], 0.1)
        self.assertAlmostEqual(out["영업이익률"][0], 0.1)
        self.assertAlmostEqual(out["부채비율"][0], 0.5)
        self.assertAlmostEqual(out["유동비율"][0], 2.0)
        self.assertAlmostEqual(out["자기자본이익률(ROE)"][0], 0.025)
        bad = cl.indicators({"연도별": [{"연도": 2025, "매출": 100, "자본": -5, "순이익": 1}]})
        self.assertIsNone(bad["자기자본이익률(ROE)"][0])
        self.assertIsNone(bad["3년 매출 증가율(연평균)"][0])

    def test_salary_estimate(self):
        est, note = cl.salary_estimate(100 * 4_000_000 * 0.095, 100, "202607")
        self.assertEqual(est["월"], 4_000_000)
        self.assertIn("추정", note)
        self.assertNotIn("상한", note)
        est, note = cl.salary_estimate(100 * 6_500_000 * 0.095, 100, "202607")
        self.assertIn("상한", note)
        est, note = cl.salary_estimate(9 * 4_000_000 * 0.095, 9, "202607")
        self.assertIsNone(est)
        self.assertEqual(cl.nps_rate(2025), 0.09)
        self.assertEqual(cl.nps_rate(2040), 0.13)
        self.assertEqual(cl.nps_cap(2026, 6), 6_370_000)
        self.assertEqual(cl.nps_cap(2026, 7), 6_590_000)


class RunTest(Base):
    def test_full_lookup_writes_result_without_key(self):
        net = FakeNet([("00000001", "가나다", ""), ("00000009", "가나다라마", "")])
        code, out = self.run_query({"회사명": "(주)가나다", "폴더": "가나다"}, net)
        self.assertEqual(code, 0, out)
        md = (self.tmp / "work" / "회사" / "가나다" / "공공자료.md").read_text(encoding="utf-8")
        js = (self.tmp / "work" / "회사" / "가나다" / "공공자료.json").read_text(encoding="utf-8")
        for text in (md, js, out):
            self.assertNotIn(DART_KEY, text)
            self.assertNotIn("Zx9", text)
        self.assertIn("영업이익률 | 10.0%", md)
        self.assertIn("약 400만원(연 4,800만원) — 추정", md)
        self.assertIn("국세청: 계속사업자", md)
        self.assertIn("상시 인원 118명", md)
        self.assertIn("금융위원회 기업개요: 종업원 120명, 평균 근속 4.2년, 1인 평균 급여 5,500만원", md)
        self.assertIn("| 2026-06 | 98 | 1 | 0 |", md)
        self.assertIn("| 2026-07 | 100 | 3 | 2 |", md)
        self.assertIn("국민연금: 사업장 1곳, 가입자 합계 100명", md)
        self.assertIn("오픈다트 | 성공 (일부: 자료 없음 — 직원 현황 없음", md)
        # 요청에는 회사명·식별번호만 실린다
        sent = " ".join(u + json.dumps(d or {}) for u, d in net.calls)
        self.assertNotIn("개발", urllib.parse.unquote(sent))
        self.assertLessEqual(len(net.calls), cl.MAX_CALLS)

    def test_same_name_twice_asks_user(self):
        net = FakeNet([("00000001", "가나다(주)", ""), ("00000002", "가나다", "")])
        with mock.patch.object(cl, "search_fsc", return_value=[]), mock.patch.object(cl, "search_nps", return_value=[]):
            code, out = self.run_query({"회사명": "가나다", "폴더": "가나다"}, net)
            self.assertEqual(code, 3, out)
            table = cl.CANDIDATES_MD.read_text(encoding="utf-8")
            self.assertIn("| 1 |", table)
            self.assertIn("| 2 |", table)
            code, out = self.run_query({"회사명": "가나다", "폴더": "가나다", "후보번호": 2}, net)
        self.assertEqual(code, 0, out)
        md = (self.tmp / "work" / "회사" / "가나다" / "공공자료.md").read_text(encoding="utf-8")
        self.assertIn("법인등록번호: 1101110000002", md)

    def test_missing_keys_still_fetch_job_board(self):
        jobs = json.dumps({"jobs": [{"id": 1, "title": "Engineer", "location": {"name": "Seoul, Korea"}, "departments": [],
                                     "first_published": "2026-09-01T00:00:00Z", "absolute_url": "https://example.com/1", "content": "&lt;p&gt;Hi&lt;/p&gt;"}]}).encode()
        cl.QUERY.write_text(json.dumps({"회사명": "Acme", "폴더": "acme-eng", "그린하우스": "acme", "저장할공고": "1"}), encoding="utf-8")
        out = io.StringIO()
        with self.keys(data=None, dart=None), mock.patch.object(cl, "fetch", return_value=jobs), contextlib.redirect_stdout(out):
            self.assertEqual(cl.run(), 0, out.getvalue())
        posting = next((self.tmp / "postings").glob("*.md")).read_text(encoding="utf-8")
        self.assertTrue(posting.startswith("게시일(그린하우스 first_published 값): 2026-09-01"))
        self.assertIn("Hi", posting)

    def test_bad_query_values(self):
        for query in ({"폴더": "x"}, {"회사명": "가", "폴더": "x", "사업자등록번호": "123"},
                      {"회사명": "가", "폴더": "x", "그린하우스": "../etc"}):
            cl.QUERY.write_text(json.dumps(query, ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(_common.UserError):
                cl.load_query()


if __name__ == "__main__":
    unittest.main()
