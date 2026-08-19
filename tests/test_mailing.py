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
