"""화면별 처리 — 파일 하나가 탭 하나다.

주소는 `@라우트(방법, 주소, 권한=…)` 로 등록한다 (`cvtool/web/router.py`).
로그인·권한 확인은 `Handler._처리` 가 **부르기 전에** 한다. 함수 안의 검사는
그보다 좁은 것(현업은 자기 과제 지원자만 등)뿐이다. 함수의 `self` 는 요청을
받은 Handler 다 (`self._send` · `self._redirect` · `self._json` …).

    login.py       로그인 · 로그아웃
    candidates.py  인재 Pool · 지원자 추가 · 지원자 상세 · 표에서 바로 고치기
    recruit.py     채용 현황
    mail.py        메일
    dashboard.py   대시보드 (블록 · 수식 · 시트)
    names.py       명칭 관리
    fields.py      표 항목
    match.py       과제 매칭
    org.py         계정 · 부서·과제 · 변경 이력

새 파일을 만들면 `app.py` 의 `_모듈차례` 에 넣어야 불린다 (의존 차례대로).
"""
