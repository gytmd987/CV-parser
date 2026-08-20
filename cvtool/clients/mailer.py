"""메일 발송 구현을 고른다.

사내 API 는 서버마다 요구 형식이 다르다. `mail_local.py` 를 두면 **그쪽이 이긴다.**
그 파일은 git 이 추적하지 않으므로(`.gitignore`) `git pull` 로 덮어써지지 않고,
이 저장소가 `mail.py` 를 고쳐도 서버 쪽 구현은 그대로 남는다.

서버에서 한 번만:
    cp cvtool/clients/mail.py cvtool/clients/mail_local.py   # 고친 파일을 옮기고
    git checkout -- cvtool/clients/mail.py                   # 원본은 되돌린다

구현 모듈은 아래를 갖추면 된다 (mail.py 가 그 본보기다).
    send(받는사람, 제목, 본문, *, html=False, 참조=None, 첨부=None, dry_run=None)
    build_url(base, user_id) / build_payload(...) / missing_settings() / MailError
"""

from __future__ import annotations

try:  # 서버에 맞춘 구현이 있으면 그것을 쓴다
    from . import mail_local as _impl

    LOCAL = True
except ImportError:  # 없으면 기본 구현
    from . import mail as _impl

    LOCAL = False

MailError = _impl.MailError
send = _impl.send
build_url = _impl.build_url
build_payload = _impl.build_payload
missing_settings = _impl.missing_settings
SendResult = getattr(_impl, "SendResult", None)

#: 화면에 어떤 구현을 쓰는지 알려주기 위한 이름
IMPL_NAME = "mail_local.py (서버 전용)" if LOCAL else "mail.py (기본)"
