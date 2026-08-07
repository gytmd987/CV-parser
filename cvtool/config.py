"""환경 설정.

값은 환경변수로 덮어쓸 수 있습니다. 기본값은 요청서(온프레미스 서버) 기준입니다.
비밀번호 등 민감 값은 코드에 하드코딩하지 말고 .env / 환경변수로 넣으세요.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


@dataclass(frozen=True)
class Settings:
    # --- 로컬 LLM (vLLM, OpenAI 호환) : 모델을 새로 올리지 말 것 ---
    llm_base_url: str = _env("CVTOOL_LLM_BASE_URL", "http://localhost:8000/v1")
    llm_model: str = _env("CVTOOL_LLM_MODEL", "thinkingcap")
    llm_api_key: str = _env("CVTOOL_LLM_API_KEY", "EMPTY")  # 인증 없음
    llm_timeout: float = float(_env("CVTOOL_LLM_TIMEOUT", "180"))

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

    # --- 시간대 (서버 시계는 UTC, 표시는 항상 KST) ---
    timezone: str = _env("CVTOOL_TZ", "Asia/Seoul")


settings = Settings()
