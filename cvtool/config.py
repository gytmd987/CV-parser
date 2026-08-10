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
    llm_max_tokens: int = int(_env("CVTOOL_LLM_MAX_TOKENS", "4096"))

    # 2단계 추출: 먼저 자유롭게 읽고 정리(추론 허용) -> 그 결과를 JSON 으로 강제.
    # guided_json 은 첫 토큰부터 문법을 강제해서 추론 모델이 생각할 자리를 없앤다.
    # 끄면 기존처럼 곧바로 구조화 추출한다(비교용).
    two_stage: bool = _env("CVTOOL_TWO_STAGE", "1").lower() not in ("0", "false", "no", "")

    # LLM 에 한 번에 보낼 CV 본문 최대 길이(문자). 넘으면 잘리므로 경고를 남긴다.
    # 서버의 --max-model-len 에 맞춰 조정하세요. `cvtool health` 가 값을 알려준다.
    max_input_chars: int = int(_env("CVTOOL_MAX_INPUT_CHARS", "24000"))

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

    # --- 개인정보 보관 기간 (채용 종료 후 N개월) ---
    retention_months: int = int(_env("CVTOOL_RETENTION_MONTHS", "6"))

    # CV 원문 텍스트를 DB 에 보관할지. 기본은 끔(개인정보 최소 수집).
    # 켜면 재업로드 없이 재분석할 수 있지만 CV 전문이 DB 에 남는다.
    store_cv_text: bool = _env("CVTOOL_STORE_CV_TEXT", "0").lower() in ("1", "true", "yes")

    # --- 시간대 (서버 시계는 UTC, 표시는 항상 KST) ---
    timezone: str = _env("CVTOOL_TZ", "Asia/Seoul")


settings = Settings()
