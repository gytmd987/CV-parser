"""사내 메일 API 호출.

요청 방식이 보통과 달라서 그대로 지켰다.

  1. **JSON 을 문자열로 직렬화해 본문에 그대로** 담아 POST 한다.
  2. `userId` 는 **URL 문자열에 직접 붙인다.** requests 의 `params=` 로 넘기면
     API 가 못 읽는다고 해서, 쿼리 문자열을 손으로 만든다.
  3. 파라미터 이름은 **대소문자까지 정확히 `userId`** 여야 한다.

표준 라이브러리만 쓴다(urllib). urllib 은 URL 을 넘긴 문자열 그대로 쓰기 때문에
2번 조건이 저절로 지켜진다.

⚠️ 실제 발송은 `MAIL_DRY_RUN=0` 일 때만 한다. 기본은 켜짐(보내지 않음)이다.
설정이 덜 된 상태로 지원자에게 메일이 나가면 되돌릴 수 없다.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

from ..config import settings


class MailError(RuntimeError):
    """메일 API 호출 실패. 메시지를 그대로 화면에 보여준다."""


@dataclass
class SendResult:
    보냄: bool          # 실제로 API 를 불렀는지 (dry-run 이면 False)
    상태코드: int
    응답: str
    요청URL: str
    본문: str


def build_url(base: str, user_id: str) -> str:
    """`?userId=...` 를 URL 문자열에 직접 붙인다.

    이미 쿼리가 있으면 `&` 로 잇는다. 값은 인코딩하지 않는다 — 사내 ID 는
    영숫자라 그대로 가는 게 API 가 읽기에 안전하다.
    """
    if not user_id:
        return base
    구분 = "&" if "?" in base else "?"
    return f"{base}{구분}userId={user_id}"


def build_payload(받는사람: str, 제목: str, 본문: str, *, html: bool = False) -> dict:
    """요청 본문에 담을 JSON.

    ⚠️ 필드 이름은 사내 API 명세에 맞춰야 한다. 명세를 받으면 이 함수만 고치면
    된다 — 나머지 코드는 이 모양을 모른다.
    """
    payload = {
        "systemId": settings.mail_api_system_id,
        "userId": settings.mail_api_user_id,
        "receiver": 받는사람,
        "title": 제목,
        "contents": 본문,
        "contentType": "HTML" if html else "TEXT",
    }
    if settings.mail_sender:
        payload["sender"] = settings.mail_sender
    return payload


def missing_settings() -> list[str]:
    """비어 있어서 발송을 막아야 하는 설정 이름들."""
    필요 = {
        "MAIL_API_URL": settings.mail_api_url,
        "MAIL_API_TOKEN": settings.mail_api_token,
        "MAIL_API_SYSTEM_ID": settings.mail_api_system_id,
        "MAIL_API_USER_ID": settings.mail_api_user_id,
    }
    return [k for k, v in 필요.items() if not str(v).strip()]


def send(받는사람: str, 제목: str, 본문: str, *, html: bool = False,
         dry_run: bool | None = None) -> SendResult:
    """메일 한 통. 실패하면 MailError."""
    if not (받는사람 or "").strip():
        raise MailError("받는 사람 주소가 없습니다.")

    payload = build_payload(받는사람.strip(), 제목, 본문, html=html)
    body = json.dumps(payload, ensure_ascii=False)
    url = build_url(settings.mail_api_url, settings.mail_api_user_id)

    if dry_run is None:
        dry_run = settings.mail_dry_run
    if dry_run:
        return SendResult(False, 0, "MAIL_DRY_RUN=1 이라 보내지 않았습니다.", url, body)

    빠진것 = missing_settings()
    if 빠진것:
        raise MailError(f"메일 설정이 비어 있습니다: {', '.join(빠진것)} (.env 를 확인하세요)")

    req = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Authorization": f"Bearer {settings.mail_api_token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=settings.mail_timeout) as resp:
            응답 = resp.read().decode("utf-8", "replace")
            return SendResult(True, resp.status, 응답, url, body)
    except urllib.error.HTTPError as exc:
        상세 = exc.read().decode("utf-8", "replace")[:500]
        raise MailError(f"메일 API 오류 {exc.code}: {상세}") from exc
    except urllib.error.URLError as exc:
        raise MailError(f"메일 API 에 연결하지 못했습니다: {exc.reason}") from exc
