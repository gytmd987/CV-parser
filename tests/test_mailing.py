"""메일 템플릿·발송 규칙·사내 API 호출 형식."""

from __future__ import annotations

import dataclasses
import json

import pytest

from cvtool import config as config_mod
from cvtool.clients import mail as mailapi
from cvtool.mailing import MailStore, render


@pytest.fixture
def ms(tmp_path):
    return MailStore(tmp_path / "m.db")


@pytest.fixture
def 템플릿(ms):
    tid = ms.add_template("서류합격", "[{{부서}}] 서류 결과", "안녕하세요 {{이름}}님",
                          만든이="admin")
    return ms.template(tid)


@pytest.fixture
def 탈락(ms):
    tid = ms.add_template("불합격 안내", "결과 안내", "{{이름}}님께", 탈락메일=True)
    return ms.template(tid)


# --- 템플릿 ------------------------------------------------------------------
def test_template_saved_and_listed(ms, 템플릿):
    assert [t.이름 for t in ms.templates()] == ["서류합격"]
    assert 템플릿.제목 == "[{{부서}}] 서류 결과"


def test_template_names_are_unique(ms, 템플릿):
    with pytest.raises(ValueError):
        ms.add_template("서류합격")


def test_template_rename_rejects_a_taken_name(ms, 템플릿):
    두번째 = ms.add_template("면접안내")
    with pytest.raises(ValueError):
        ms.update_template(두번째, 이름="서류합격")


def test_placeholders_found_in_subject_and_body(템플릿):
    assert 템플릿.placeholders() == ["부서", "이름"]


def test_deleting_a_template_keeps_the_sent_log(ms, 템플릿):
    ms.record("CV-1", 템플릿, "a@b.com", "제목", "본문", "성공")
    ms.delete_template(템플릿.id)
    assert ms.templates() == []
    assert len(ms.history()) == 1        # 누구에게 뭘 보냈는지는 기록이다


# --- 지원자별 맞춤 ------------------------------------------------------------
def test_render_fills_each_applicant_in():
    글, 빈칸 = render("안녕하세요 {{이름}}님", {"이름": "홍길동"})
    assert 글 == "안녕하세요 홍길동님" and 빈칸 == []


def test_render_reports_empty_values():
    글, 빈칸 = render("안녕하세요 {{이름}}님 ({{부서}})", {"이름": "홍길동", "부서": ""})
    assert 빈칸 == ["부서"]
    assert 글 == "안녕하세요 홍길동님 ()"


def test_render_tolerates_spaces_in_the_placeholder():
    글, _ = render("{{ 이름 }}님", {"이름": "홍길동"})
    assert 글 == "홍길동님"


def test_unknown_placeholder_becomes_empty_and_is_reported():
    글, 빈칸 = render("{{없는것}}", {"이름": "홍길동"})
    assert 글 == "" and 빈칸 == ["없는것"]


# --- 발송 규칙 ----------------------------------------------------------------
def test_same_template_only_once_per_applicant(ms, 템플릿):
    assert ms.blocked_reason("CV-1", 템플릿) == ""
    ms.record("CV-1", 템플릿, "a@b.com", "제목", "본문", "성공")
    assert "이미 받았습니다" in ms.blocked_reason("CV-1", 템플릿)


def test_other_applicants_are_unaffected(ms, 템플릿):
    ms.record("CV-1", 템플릿, "a@b.com", "제목", "본문", "성공")
    assert ms.blocked_reason("CV-2", 템플릿) == ""


def test_failed_send_can_be_retried(ms, 템플릿):
    ms.record("CV-1", 템플릿, "a@b.com", "제목", "본문", "실패", 오류="서버 오류")
    assert ms.blocked_reason("CV-1", 템플릿) == ""


def test_rejection_mail_blocks_everything_after(ms, 템플릿, 탈락):
    ms.record("CV-1", 탈락, "a@b.com", "결과", "본문", "성공")
    assert "탈락 메일" in ms.blocked_reason("CV-1", 템플릿)
    assert "탈락 메일" in ms.blocked_reason("CV-1", 탈락)


def test_rejection_only_blocks_that_applicant(ms, 템플릿, 탈락):
    ms.record("CV-1", 탈락, "a@b.com", "결과", "본문", "성공")
    assert ms.blocked_reason("CV-2", 템플릿) == ""


def test_dry_run_still_counts_as_sent(ms, 템플릿):
    """연습 모드 기록도 '보냄' 으로 친다. 안 그러면 진짜로 보낼 때 두 번 간다."""
    ms.record("CV-1", 템플릿, "a@b.com", "제목", "본문", "발송안함")
    assert "이미 받았습니다" in ms.blocked_reason("CV-1", 템플릿)


def test_sent_and_rejected_id_sets(ms, 템플릿, 탈락):
    ms.record("CV-1", 템플릿, "a@b.com", "제목", "본문", "성공")
    ms.record("CV-2", 탈락, "b@b.com", "제목", "본문", "성공")
    assert ms.sent_ids(템플릿.id) == {"CV-1"}
    assert ms.rejected_ids() == {"CV-2"}


# --- 사내 API 호출 형식 --------------------------------------------------------
def test_user_id_is_appended_to_the_url_string():
    """requests 의 params= 로 넘기면 API 가 못 읽는다고 해서 URL 에 직접 붙인다."""
    assert mailapi.build_url("https://mail/api/send", "hong12") \
        == "https://mail/api/send?userId=hong12"


def test_user_id_joins_an_existing_query_string():
    assert mailapi.build_url("https://mail/api/send?a=1", "hong12") \
        == "https://mail/api/send?a=1&userId=hong12"


def test_user_id_parameter_is_case_sensitive():
    url = mailapi.build_url("https://mail/api/send", "hong12")
    assert "userId=" in url and "userid=" not in url and "UserId=" not in url


def test_no_user_id_leaves_the_url_alone():
    assert mailapi.build_url("https://mail/api/send", "") == "https://mail/api/send"


def test_body_is_a_json_string(monkeypatch):
    _설정(monkeypatch, mail_api_url="https://mail/api/send", mail_api_user_id="hong12",
        mail_api_system_id="CVTOOL", mail_api_token="tok")
    결과 = mailapi.send("a@b.com", "제목", "본문", dry_run=True)
    payload = json.loads(결과.본문)              # 본문은 JSON 문자열이어야 한다
    assert payload["receiver"] == "a@b.com"
    assert payload["systemId"] == "CVTOOL"
    assert 결과.요청URL.endswith("?userId=hong12")


def test_dry_run_does_not_call_the_api(monkeypatch):
    def 폭발(*a, **k):
        raise AssertionError("연습 모드에서는 API 를 부르면 안 된다")

    monkeypatch.setattr(mailapi.urllib.request, "urlopen", 폭발)
    결과 = mailapi.send("a@b.com", "제목", "본문", dry_run=True)
    assert 결과.보냄 is False


def test_missing_settings_are_named(monkeypatch):
    _설정(monkeypatch, mail_api_url="", mail_api_token="", mail_api_system_id="",
        mail_api_user_id="")
    assert set(mailapi.missing_settings()) == {
        "MAIL_API_URL", "MAIL_API_TOKEN", "MAIL_API_SYSTEM_ID", "MAIL_API_USER_ID",
    }
    with pytest.raises(mailapi.MailError) as exc:
        mailapi.send("a@b.com", "제목", "본문", dry_run=False)
    assert "MAIL_API_URL" in str(exc.value)


def test_empty_recipient_is_refused():
    with pytest.raises(mailapi.MailError):
        mailapi.send("  ", "제목", "본문", dry_run=True)


def test_dry_run_is_on_by_default():
    """설정을 맞추기 전에 지원자에게 메일이 나가면 되돌릴 수 없다."""
    assert config_mod.settings.mail_dry_run is True


def _설정(monkeypatch, **값들):
    monkeypatch.setattr(mailapi, "settings",
                        dataclasses.replace(config_mod.settings, **값들))


# --- 화면 --------------------------------------------------------------------
def test_history_is_per_applicant(ms, 템플릿):
    ms.record("CV-1", 템플릿, "a@b.com", "제목", "본문", "성공")
    ms.record("CV-2", 템플릿, "b@b.com", "제목", "본문", "성공")
    assert [r["지원자_ID"] for r in ms.history("CV-1")] == ["CV-1"]
    assert ms.count() == 2


# --- 참조(CC) ------------------------------------------------------------------
def test_cc_saved_on_the_template(ms):
    tid = ms.add_template("안내", 참조="hr@corp.com; team@corp.com")
    assert ms.template(tid).cc() == ["hr@corp.com", "team@corp.com"]


def test_cc_accepts_commas_semicolons_and_newlines():
    from cvtool.mailing import split_addresses

    assert split_addresses("a@x.com, b@x.com;c@x.com\nd@x.com") == [
        "a@x.com", "b@x.com", "c@x.com", "d@x.com",
    ]
    assert split_addresses("  ") == []


def test_cc_goes_into_the_payload():
    p = mailapi.build_payload("a@b.com", "제목", "본문", 참조=["c@x.com"])
    assert p["cc"] == ["c@x.com"]


def test_no_cc_field_when_empty():
    assert "cc" not in mailapi.build_payload("a@b.com", "제목", "본문")


def test_cc_is_recorded_in_the_log(ms):
    tid = ms.add_template("안내", 참조="hr@corp.com")
    tpl = ms.template(tid)
    ms.record("CV-1", tpl, "a@b.com", "제목", "본문", "성공", 참조="hr@corp.com")
    assert ms.history("CV-1")[0]["참조"] == "hr@corp.com"


# --- 첨부파일 -------------------------------------------------------------------
def test_attachment_saved_under_the_template(ms, 템플릿):
    ms.add_attachment(템플릿.id, "안내문.pdf", b"%PDF-1.4", 올린이="admin")
    붙은것 = ms.attachments(템플릿.id)
    assert [a["파일명"] for a in 붙은것] == ["안내문.pdf"]
    assert 붙은것[0]["저장명"].startswith(f"MT{템플릿.id}-")   # 이름은 템플릿 기준
    assert ms.attachment_bytes(템플릿.id) == [("안내문.pdf", b"%PDF-1.4")]


def test_attachment_file_is_owner_only(ms, 템플릿):
    from cvtool.fsutil import is_world_readable

    ms.add_attachment(템플릿.id, "안내문.pdf", b"%PDF")
    저장명 = ms.attachments(템플릿.id)[0]["저장명"]
    assert not is_world_readable(ms.files_dir / 저장명)


def test_executable_attachments_are_refused(ms, 템플릿):
    with pytest.raises(ValueError):
        ms.add_attachment(템플릿.id, "evil.exe", b"MZ")


def test_huge_attachment_is_refused(ms, 템플릿):
    from cvtool.mailing import MAX_ATTACHMENT_BYTES

    with pytest.raises(ValueError):
        ms.add_attachment(템플릿.id, "big.pdf", b"x" * (MAX_ATTACHMENT_BYTES + 1))


def test_deleting_an_attachment_removes_the_file(ms, 템플릿):
    ms.add_attachment(템플릿.id, "안내문.pdf", b"%PDF")
    저장명 = ms.attachments(템플릿.id)[0]["저장명"]
    ms.delete_attachment(ms.attachments(템플릿.id)[0]["id"])
    assert ms.attachments(템플릿.id) == []
    assert not (ms.files_dir / 저장명).exists()


def test_deleting_a_template_removes_its_attachments(ms, 템플릿):
    ms.add_attachment(템플릿.id, "안내문.pdf", b"%PDF")
    저장명 = ms.attachments(템플릿.id)[0]["저장명"]
    ms.delete_template(템플릿.id)
    assert not (ms.files_dir / 저장명).exists()


def test_attachments_are_base64_in_the_payload():
    import base64

    p = mailapi.build_payload("a@b.com", "제목", "본문", 첨부=[("a.pdf", b"%PDF-1.4")])
    assert p["attachments"][0]["fileName"] == "a.pdf"
    assert base64.b64decode(p["attachments"][0]["content"]) == b"%PDF-1.4"


# --- 꾸민 본문 -----------------------------------------------------------------
def test_new_templates_are_html(ms):
    tid = ms.add_template("안내")
    assert ms.template(tid).html is True


def test_html_flag_reaches_the_payload():
    p = mailapi.build_payload("a@b.com", "제목", "<b>본문</b>", html=True)
    assert p["contentType"] == "HTML"
    assert mailapi.build_payload("a@b.com", "제목", "본문")["contentType"] == "TEXT"


def test_placeholders_are_found_inside_markup(ms):
    tid = ms.add_template("안내", "제목", "<p>안녕하세요 <b>{{이름}}</b>님</p>")
    assert ms.template(tid).placeholders() == ["이름"]


def test_render_keeps_the_markup():
    글, _ = render("<p>안녕하세요 <b>{{이름}}</b>님</p>", {"이름": "홍길동"})
    assert 글 == "<p>안녕하세요 <b>홍길동</b>님</p>"


def test_html_to_text_for_previews():
    from cvtool.mailing import html_to_text

    assert html_to_text("<p>안녕<br>하세요</p><p>반갑습니다</p>") == "안녕\n하세요\n\n반갑습니다"
    assert html_to_text("<b>가</b>&nbsp;나") == "가 나"


def test_old_text_templates_keep_working(tmp_path):
    """본문형식 열이 없던 DB 는 TEXT 로 열려야 한다 (HTML 로 잘못 보내면 안 됨)."""
    import sqlite3

    path = tmp_path / "m.db"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE templates (id INTEGER PRIMARY KEY AUTOINCREMENT, 이름 TEXT NOT NULL"
        " UNIQUE, 제목 TEXT DEFAULT '', 본문 TEXT DEFAULT '', 탈락메일 INTEGER DEFAULT 0,"
        " 만든이 TEXT DEFAULT '', 만든일시 TEXT DEFAULT '', 수정일시 TEXT DEFAULT '');"
        "CREATE TABLE sent (id INTEGER PRIMARY KEY AUTOINCREMENT, 지원자_ID TEXT NOT NULL,"
        " template_id INTEGER NOT NULL, 템플릿이름 TEXT DEFAULT '', 받는사람 TEXT DEFAULT '',"
        " 제목 TEXT DEFAULT '', 본문 TEXT DEFAULT '', 상태 TEXT DEFAULT '성공',"
        " 탈락메일 INTEGER DEFAULT 0, 오류 TEXT DEFAULT '', 보낸이 TEXT DEFAULT '',"
        " 보낸일시 TEXT DEFAULT '');"
    )
    conn.execute("INSERT INTO templates (이름, 제목, 본문) VALUES ('옛것','제목','본문')")
    conn.commit(); conn.close()

    ms = MailStore(path)
    옛것 = ms.template_by_name("옛것")
    assert 옛것.html is False and 옛것.참조 == ""


# --- 진짜로 나가는지 (가짜 API 를 띄워서 확인) ---------------------------------
@pytest.fixture
def 가짜API():
    """받은 요청을 그대로 담아두는 최소 서버."""
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    받은것: list[dict] = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            받은것.append({
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "ctype": self.headers.get("Content-Type"),
                "body": self.rfile.read(n).decode("utf-8"),
            })
            out = _json.dumps({"resultCode": "SUCCESS"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/api/send", 받은것
    srv.shutdown()


def test_dry_run_off_actually_posts(monkeypatch, 가짜API):
    url, 받은것 = 가짜API
    _설정(monkeypatch, mail_api_url=url, mail_api_token="tok-123",
        mail_api_system_id="CVTOOL", mail_api_user_id="hong12", mail_dry_run=False)

    결과 = mailapi.send("a@b.com", "제목", "<b>본문</b>", html=True,
                      참조=["hr@corp.com"], 첨부=[("안내문.pdf", b"%PDF")])

    assert 결과.보냄 is True and 결과.상태코드 == 200
    assert "SUCCESS" in 결과.응답
    assert len(받은것) == 1
    요청 = 받은것[0]
    assert 요청["path"] == "/api/send?userId=hong12"      # URL 에 직접 붙는다
    assert 요청["auth"] == "Bearer tok-123"
    assert 요청["ctype"].startswith("application/json")
    보낸본문 = json.loads(요청["body"])
    assert 보낸본문["receiver"] == "a@b.com"
    assert 보낸본문["contentType"] == "HTML"
    assert 보낸본문["cc"] == ["hr@corp.com"]
    assert 보낸본문["attachments"][0]["fileName"] == "안내문.pdf"


def test_dry_run_on_sends_nothing_even_with_a_live_api(monkeypatch, 가짜API):
    url, 받은것 = 가짜API
    _설정(monkeypatch, mail_api_url=url, mail_api_token="tok",
        mail_api_system_id="S", mail_api_user_id="u", mail_dry_run=True)
    결과 = mailapi.send("a@b.com", "제목", "본문")
    assert 결과.보냄 is False
    assert 받은것 == []                      # 서버가 살아 있어도 안 보낸다


def test_api_error_becomes_a_readable_message(monkeypatch):
    _설정(monkeypatch, mail_api_url="http://127.0.0.1:1/none", mail_api_token="t",
        mail_api_system_id="S", mail_api_user_id="u", mail_dry_run=False)
    with pytest.raises(mailapi.MailError) as exc:
        mailapi.send("a@b.com", "제목", "본문")
    assert "연결하지 못했습니다" in str(exc.value)
