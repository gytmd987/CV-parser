"""환경 설정.

값은 환경변수로 덮어쓸 수 있습니다. 기본값은 요청서(온프레미스 서버) 기준입니다.
비밀번호 등 민감 값은 코드에 하드코딩하지 말고 .env / 환경변수로 넣으세요.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# .env 를 os.environ 에 먼저 반영한다. 아래 기본값들이 계산되기 전에 실행돼야 하므로
# 이 임포트 위치를 바꾸지 말 것.
from .dotenv import LOADED_FROM  # noqa: F401


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    # --- 로컬 LLM (vLLM, OpenAI 호환) : 모델을 새로 올리지 말 것 ---
    llm_base_url: str = _env("CVTOOL_LLM_BASE_URL", "http://localhost:8000/v1")
    llm_model: str = _env("CVTOOL_LLM_MODEL", "thinkingcap")
    llm_api_key: str = _env("CVTOOL_LLM_API_KEY", "EMPTY")  # 인증 없음
    llm_timeout: float = float(_env("CVTOOL_LLM_TIMEOUT", "180"))
    # 명시하지 않으면 서버 설정에 따라 출력이 조용히 잘린다.
    #
    # 이 값은 **한 번에 생성할 출력 상한**이지 목표치가 아니다. 모델은 답이
    # 끝나면 멈추므로 크게 잡아도 평소 속도에는 영향이 없다. 반대로 작으면
    # 논문 많은 사람이나 과제 많은 매칭에서 **JSON 이 중간에 잘려 통째로**
    # 날아간다. 정확도가 먼저라 넉넉히 잡는다.
    llm_max_tokens: int = int(_env("CVTOOL_LLM_MAX_TOKENS", "32768"))

    # 2단계 추출: 먼저 자유롭게 읽고 정리(추론 허용) -> 그 결과를 JSON 으로 강제.
    # guided_json 은 첫 토큰부터 문법을 강제해서 추론 모델이 생각할 자리를 없앤다.
    # 끄면 기존처럼 곧바로 구조화 추출한다(비교용).
    two_stage: bool = _env("CVTOOL_TWO_STAGE", "1").lower() not in ("0", "false", "no", "")

    # 한 번에 추출: 인적사항·학력·연구·경력을 **한 호출로** 받는다.
    #
    # 나눠 부르면 2~5번째 호출이 CV 전문을 매번 다시 보낸다. CV 가 8천 토큰이면
    # 입력만 4만 토큰이고, 느린 원인의 대부분이 여기다. 합치면 원문을 한 번만
    # 보내므로 분석 시간이 절반 아래로 떨어진다.
    #
    # 기본을 끔으로 둔 이유: 스키마가 커지면 뒤쪽 항목(연구분야_키워드,
    # 석박통합 같은 것)에 주의가 덜 가는 경향이 있다. **정확도가 유지되는지는
    # 실제 CV 로 확인해야** 알 수 있고, 그건 이 저장소에서 검증할 수 없다.
    # 서버에서 같은 CV 를 양쪽으로 돌려 비교한 뒤 기본값을 바꾸세요.
    #
    # 켜도 안전장치는 남는다. 답이 잘리면(LLMTruncated) 저절로 네 섹션으로
    # 나눠 다시 부른다 — 느려질 뿐 틀리지 않는다.
    oneshot: bool = _env("CVTOOL_EXTRACT_ONESHOT", "0").lower() not in ("0", "false", "no", "")

    # LLM 에 보낼 CV 본문 최대 길이(문자). 0 이면 제한 없음(기본).
    # 이 서버는 컨텍스트가 커서 자를 필요가 없다. 컨텍스트가 작은 모델로
    # 바꿀 때만 값을 넣으세요.
    max_input_chars: int = int(_env("CVTOOL_MAX_INPUT_CHARS", "0"))

    # 추론 모델은 temperature 0 에서 반복에 빠지기도 한다. 답변이 이상하면 조정.
    llm_temperature: float = float(_env("CVTOOL_LLM_TEMPERATURE", "0.0"))

    # --- TEI 임베딩 / 리랭커 (다음 슬라이스에서 사용) ---
    embed_url: str = _env("CVTOOL_EMBED_URL", "http://localhost:8081")
    embed_dim: int = int(_env("CVTOOL_EMBED_DIM", "1024"))  # KURE-v1
    embed_batch: int = int(_env("CVTOOL_EMBED_BATCH", "32"))  # 32개씩 잘라 보내야 함
    rerank_url: str = _env("CVTOOL_RERANK_URL", "http://localhost:8082")

    # --- 별도 저장소 (기존 5432/6333 금지) ---
    pg_host: str = _env("CVTOOL_PG_HOST", "127.0.0.1")
    pg_port: int = int(_env("CVTOOL_PG_PORT", "5433"))
    pg_user: str = _env("CVTOOL_PG_USER", "cvtool")
    pg_password: str = _env("CVTOOL_PG_PASSWORD", "")  # .env 로 주입
    pg_db: str = _env("CVTOOL_PG_DB", "cvtool_db")
    qdrant_url: str = _env("CVTOOL_QDRANT_URL", "http://127.0.0.1:6335")

    # --- 개인정보 보관 기간 (개월). 0 이면 무제한(자동 삭제 안 함) ---
    # 무제한이 기본이다. 파기 시점은 담당자가 직접 판단해 수동으로 삭제한다.
    retention_months: int = int(_env("CVTOOL_RETENTION_MONTHS", "0"))

    # CV 원문 텍스트를 DB 에 보관할지. 기본은 끔(개인정보 최소 수집).
    # 켜면 재업로드 없이 재분석할 수 있지만 CV 전문이 DB 에 남는다.
    store_cv_text: bool = _env("CVTOOL_STORE_CV_TEXT", "0").lower() in ("1", "true", "yes")

    # --- 연구 과제 매칭 ---
    # 과제 정보 JSON 경로. 상대경로는 **저장소 폴더 기준**으로 푼다
    # (CV-parser 에서 `cd ../과제정보` 로 가는 곳이면 `../과제정보/과제.json`).
    projects_json: str = _env("CVTOOL_PROJECTS_JSON", "")
    # 원본에서 필요한 과제·필드만 골라 저장하는 파일. 비우면 데이터 폴더에 만든다.
    # 이 파일이 있으면 매칭은 **원본 대신 이 파일**을 쓴다.
    projects_curated: str = _env("CVTOOL_PROJECTS_CURATED", "")
    # 한 번에 물어볼 과제 수. **과제는 전부 비교하고**, 답이 잘리지 않게 나눠 묻는다.
    # 한 번에 몇 개 과제를 물어볼지. **0 이면 전부 한 번에** 넣는다(기본).
    #
    # 나눠 물으면 모델이 묶음 안에서만 상대 비교를 하게 돼서, 묶음 경계를
    # 넘는 점수가 서로 안 맞는다. 한 번에 다 보여줘야 줄을 제대로 세운다.
    # 답이 잘리면 자동으로 반씩 쪼개 다시 물으므로(_한묶음 참고) 크게 잡아도
    # 결과를 잃지 않는다.
    match_batch: int = int(_env("CVTOOL_MATCH_BATCH", "0"))
    # 화면에 기본으로 보여줄 상위 과제 수 (나머지는 '전체 보기')
    match_show: int = int(_env("CVTOOL_MATCH_SHOW", "3"))
    # CV 를 분석한 뒤 매칭까지 자동으로 돌릴지. 과제 파일이 있을 때만 뜻이 있다.
    match_auto: bool = _env("CVTOOL_MATCH_AUTO", "1").lower() not in ("0", "false", "no", "")

    # --- 사내 메일 API ---
    # 토큰·ID 는 코드에 두지 않는다. .env 로만 넣는다.
    mail_api_url: str = _env("MAIL_API_URL", _env("CVTOOL_MAIL_API_URL", ""))
    mail_api_token: str = _env("MAIL_API_TOKEN", "")
    mail_api_system_id: str = _env("MAIL_API_SYSTEM_ID", "")
    mail_api_user_id: str = _env("MAIL_API_USER_ID", "")
    mail_sender: str = _env("MAIL_SENDER", "")
    mail_timeout: float = float(_env("MAIL_API_TIMEOUT", "30"))
    # 실제로 보내지 않고 기록만 한다. **기본이 켜짐** — 설정을 맞추기 전에
    # 지원자에게 메일이 나가는 일이 없어야 한다. 확인 뒤 0 으로 끄세요.
    mail_dry_run: bool = _env("MAIL_DRY_RUN", "1").lower() not in ("0", "false", "no", "")

    # --- 시간대 (서버 시계는 UTC, 표시는 항상 KST) ---
    timezone: str = _env("CVTOOL_TZ", "Asia/Seoul")


settings = Settings()
