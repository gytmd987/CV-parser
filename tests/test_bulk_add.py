"""엑셀 양식으로 지원자 여러 명 한 번에 등록."""

from __future__ import annotations

import importlib
import io
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer

import pytest

from cvtool import bulk
from cvtool.export import build_xlsx
from cvtool.xlsx_read import XlsxError, col_index, read_sheet


# --- xlsx 읽기 ---------------------------------------------------------------
def test_우리가_쓴_파일을_그대로_읽는다():
    data = build_xlsx([{"a": "01012345678", "b": "202403"}], ["a", "b"])
    assert read_sheet(data) == [["a", "b"], ["01012345678", "202403"]]


def test_앞자리_0_과_연월이_숫자로_안_바뀐다():
    data = build_xlsx([{"전화번호": "01012345678"}], ["전화번호"])
    assert read_sheet(data)[1][0] == "01012345678"


def test_열_이름을_거꾸로_푼다():
    assert (col_index("A"), col_index("Z"), col_index("AA"), col_index("AB")) == (
        0, 25, 26, 27)


def _엑셀_흉내(머리: list[str], 줄들: list[list[str]]) -> bytes:
    """사람이 엑셀로 열어 저장한 모양 — 공유 문자열 · 빈 칸 생략 · 쪼개진 <t>."""
    말들 = []
    for 글 in [*머리, *[v for 줄 in 줄들 for v in 줄]]:
        if 글 and 글 not in 말들:
            말들.append(글)
    si = "".join(
        # 첫 글자만 따로 떼어 <t> 를 둘로 쪼갠다 (서식이 바뀐 셀이 이렇게 나온다)
        f"<si><r><t>{글[:1]}</t></r><r><t>{글[1:]}</t></r></si>" if len(글) > 1
        else f"<si><t>{글}</t></si>"
        for 글 in 말들
    )
    shared = ('<?xml version="1.0"?><sst xmlns="http://schemas.openxmlformats.org/'
              f'spreadsheetml/2006/main" count="{len(말들)}">{si}</sst>')

    def 칸(줄번호, 값들):
        out = []
        for n, v in enumerate(값들):
            if not v:                     # 빈 칸은 아예 안 쓴다 (엑셀이 그렇게 한다)
                continue
            자리 = chr(65 + n) + str(줄번호)
            out.append(f'<c r="{자리}" t="s"><v>{말들.index(v)}</v></c>')
        return f'<row r="{줄번호}">' + "".join(out) + "</row>"

    rows = 칸(1, 머리) + "".join(칸(i, 줄) for i, 줄 in enumerate(줄들, start=2))
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/'
             f'spreadsheetml/2006/main"><sheetData>{rows}</sheetData></worksheet>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("_rels/.rels", "<Relationships/>")
        z.writestr("xl/workbook.xml",
                   '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/'
                   'spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
                   'officeDocument/2006/relationships"><sheets>'
                   '<sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
                   'openxmlformats.org/package/2006/relationships"><Relationship '
                   'Id="rId1" Type="ws" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", shared)
        z.writestr("xl/worksheets/sheet1.xml", sheet)
    return buf.getvalue()


def test_공유_문자열과_빈칸과_쪼개진_글자를_읽는다():
    data = _엑셀_흉내(["한글_이름", "이메일", "전화번호"],
                   [["홍길동", "", "01011112222"]])
    표 = read_sheet(data)
    assert 표[0] == ["한글_이름", "이메일", "전화번호"]
    # 빈 칸이 생략돼도 전화번호가 세 번째 자리에 그대로 있어야 한다
    assert 표[1] == ["홍길동", "", "01011112222"]


def test_엑셀이_아니면_알아듣게_거절한다():
    with pytest.raises(XlsxError):
        read_sheet(b"not a zip")


# --- 양식 --------------------------------------------------------------------
@pytest.fixture
def store(tmp_path):
    from cvtool.store import CandidateStore
    return CandidateStore(tmp_path / "c.db")


def test_양식에는_사람이_채울_수_있는_열만_있다(store):
    열 = bulk.양식열(store)
    for 있어야 in ("한글_이름", "생년월일", "박사_학교", "등록년도"):
        assert 있어야 in 열
    for 없어야 in ("저널_수", "학회_수", "검토_사유", "검토_필요",
                "임팩트_팩터", "1저자_해외논문_제출처", "지원자_ID"):
        assert 없어야 not in 열


def test_추가한_열도_양식에_들어간다(store):
    store.add_field("희망연봉", "텍스트", 구분="지원자 정보")
    store.add_field("면접장소", "텍스트", 구분="채용 현황")
    열 = bulk.양식열(store)
    assert "희망연봉" in 열          # 지원자 정보
    assert "면접장소" not in 열       # 채용 현황은 채용 현황 화면에서 채운다


def test_양식은_머리글만_있는_빈_엑셀이다(store):
    열 = bulk.양식열(store)
    assert read_sheet(bulk.양식(열)) == [열]


def test_보이는_이름으로_적어도_내부_이름으로_적어도_읽는다(store):
    열 = bulk.양식열(store)
    라벨 = {"경력_회사": "경력_회사(학교)"}
    for 머리 in ("경력_회사(학교)", "경력_회사"):
        data = _엑셀_흉내(["한글_이름", 머리], [["홍길동", "포스코"]])
        줄들, 모르는것 = bulk.읽기(data, 열, 라벨)
        assert 줄들[0][1]["경력_회사"] == "포스코"
        assert 모르는것 == []


def test_모르는_열은_넘기고_알려준다(store):
    data = _엑셀_흉내(["한글_이름", "혈액형"], [["홍길동", "A"]])
    줄들, 모르는것 = bulk.읽기(data, bulk.양식열(store))
    assert 모르는것 == ["혈액형"]
    assert 줄들[0][1] == {"한글_이름": "홍길동"}


def test_행_번호는_엑셀에서_보이는_번호다(store):
    data = _엑셀_흉내(["한글_이름"], [["가"], ["나"]])
    줄들, _ = bulk.읽기(data, bulk.양식열(store))
    assert [n for n, _ in 줄들] == [2, 3]      # 머리글이 1행


def test_빈_줄은_조용히_건너뛴다(store):
    data = _엑셀_흉내(["한글_이름"], [["가"], [""], ["나"]])
    줄들, _ = bulk.읽기(data, bulk.양식열(store))
    assert [값["한글_이름"] for _, 값 in 줄들] == ["가", "나"]


def test_머리글을_하나도_모르면_거절한다(store):
    data = _엑셀_흉내(["가", "나"], [["1", "2"]])
    with pytest.raises(XlsxError):
        bulk.읽기(data, bulk.양식열(store))


# --- 화면 --------------------------------------------------------------------
@pytest.fixture(scope="module")
def web(tmp_path_factory):
    data = tmp_path_factory.mktemp("cvdata")
    os.environ["CVTOOL_DATA_DIR"] = str(data)
    os.environ["CVTOOL_ADMIN_PASSWORD"] = "pw1234"
    os.environ["CVTOOL_ADMIN_ID"] = "admin"
    mod = importlib.reload(importlib.import_module("cvtool.web.app"))
    mod.bootstrap_admin()
    server = ThreadingHTTPServer(("127.0.0.1", 0), mod.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(CookieJar()))

    class Client:
        module = mod
        base = f"http://127.0.0.1:{port}"

        def post(self, path: str, **fields):
            body = urllib.parse.urlencode(fields, doseq=True, encoding="utf-8").encode()
            req = urllib.request.Request(self.base + path, data=body)
            try:
                with opener.open(req, timeout=20) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

        def get(self, path: str) -> str:
            with opener.open(self.base + path, timeout=20) as r:
                return r.read().decode("utf-8", "replace")

        def raw(self, path: str) -> bytes:
            with opener.open(self.base + path, timeout=20) as r:
                return r.read()

        def 올리기(self, data: bytes):
            경계 = "----X"
            몸 = (
                f"--{경계}\r\n"
                'Content-Disposition: form-data; name="files"; filename="a.xlsx"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode() + data + f"\r\n--{경계}--\r\n".encode()
            req = urllib.request.Request(
                self.base + "/upload/xlsx", data=몸,
                headers={"Content-Type": f"multipart/form-data; boundary={경계}"})
            try:
                with opener.open(req, timeout=30) as r:
                    return r.status, r.read().decode("utf-8", "replace")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8", "replace")

    c = Client()
    c.post("/login", userid="admin", password="pw1234")
    yield c
    server.shutdown()


def _양식(web):
    열 = bulk.양식열(web.module.store)
    return 열, web.module.라벨(열)


def test_양식을_내려받아_채워_올리면_등록된다(web):
    받은것 = web.raw("/upload/template.xlsx")
    머리 = read_sheet(받은것)[0]
    전 = len(web.module.store.list_all())
    코드, 본문 = web.올리기(_엑셀_흉내(
        머리, [["김하나"] + [""] * (len(머리) - 1),
              ["이두리"] + [""] * (len(머리) - 1)]))
    assert 코드 == 200 and "2명 등록했습니다" in 본문
    assert len(web.module.store.list_all()) == 전 + 2
    assert {"김하나", "이두리"} <= {r.한글_이름 for r in web.module.store.list_all()}


def test_올린_사람은_전부_검토_필요다(web):
    web.올리기(_엑셀_흉내(["한글_이름"], [["박검토"]]))
    rec = next(r for r in web.module.store.list_all() if r.한글_이름 == "박검토")
    assert rec.검토_필요 == "Y"
    assert bulk.등록사유 in rec.검토_사유


def test_형식이_틀린_줄만_빠진다(web):
    전 = len(web.module.store.list_all())
    코드, 본문 = web.올리기(_엑셀_흉내(
        ["한글_이름", "생년월일"],
        [["정맞음", "19900101"], ["최틀림", "19xx"], ["강맞음", "19910202"]]))
    assert 코드 == 200
    assert "2명 등록했습니다" in 본문
    assert "3행 생년월일" in 본문            # 몇 행이 왜 빠졌는지
    이름들 = {r.한글_이름 for r in web.module.store.list_all()}
    assert {"정맞음", "강맞음"} <= 이름들 and "최틀림" not in 이름들
    assert len(web.module.store.list_all()) == 전 + 2


def test_사전에_없던_학교는_명칭_관리에_미분류로_올라온다(web):
    web.올리기(_엑셀_흉내(["한글_이름", "박사_학교"], [["신입생", "듣도보도못한대학교"]]))
    표기 = [n.원표기 for n in web.module.registry.list_all("소속")]
    assert "듣도보도못한대학교" in 표기
    rec = next(r for r in web.module.store.list_all() if r.한글_이름 == "신입생")
    assert rec.박사_학교 == "듣도보도못한대학교"      # 원문 표기를 그대로 담는다


def test_이미_있는_사람과_이메일이_같으면_중복으로_표시된다(web):
    web.올리기(_엑셀_흉내(["한글_이름", "이메일"], [["원본이", "dup@example.com"]]))
    web.올리기(_엑셀_흉내(["한글_이름", "이메일"], [["똑같이", "dup@example.com"]]))
    rec = next(r for r in web.module.store.list_all() if r.한글_이름 == "똑같이")
    assert "중복" in rec.검토_사유 and rec.검토_필요 == "Y"
    assert web.module.store.duplicate_note(rec.지원자_ID)


def test_등록년도도_들어간다(web):
    web.올리기(_엑셀_흉내(["한글_이름", "등록년도"], [["연도씨", "2019"]]))
    rec = next(r for r in web.module.store.list_all() if r.한글_이름 == "연도씨")
    assert web.module.store.year_of(rec.지원자_ID) == "2019"


def test_등록한_것이_변경_이력에_남는다(web):
    web.올리기(_엑셀_흉내(["한글_이름"], [["이력씨"]]))
    rec = next(r for r in web.module.store.list_all() if r.한글_이름 == "이력씨")
    assert any("엑셀" in e.비고 for e in web.module.audit.for_candidate(rec.지원자_ID))


def test_엑셀이_아닌_파일은_알아듣게_거절한다(web):
    코드, 본문 = web.올리기("이건 엑셀이 아니다".encode())
    assert 코드 == 200
    assert "엑셀 파일이 아닙니다" in 본문


def test_파일을_안_고르면_알려준다(web):
    코드, 본문 = web.post("/upload/xlsx")
    assert "엑셀 파일을 고른 뒤" in 본문 or 코드 == 200


def test_지원자_추가_화면에_양식_받기가_있다(web):
    쪽 = web.get("/upload")
    assert "/upload/template.xlsx" in 쪽
    assert "/upload/xlsx" in 쪽
