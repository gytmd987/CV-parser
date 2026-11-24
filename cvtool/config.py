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

    # 추론 모델의 생각(thinking) 켜기. 끄면 요청마다
    # chat_template_kwargs={"enable_thinking": false} 를 보낸다 (Qwen3 계열 · vLLM).
    # 생각을 끄면 답이 훨씬 빨리 나온다. 서버가 이 필드를 거절하면 빼고 다시 부른다.
    llm_thinking: bool = _env("CVTOOL_LLM_THINKING", "1").lower() not in ("0", "false", "no", "off", "")

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

    # --- AI 분석을 누가 하나 ---
    # server : 이 시스템이 사내 LLM(위 CVTOOL_LLM_*)을 직접 부른다
    # agent  : LLM 을 부르지 않는다. CV 분석 · 과제 매칭을 «대기» 로 쌓아 두고,
    #          사내 코워크 같은 에이전트가 MCP/API 로 가져가 판단해 돌려준다.
    llm_mode: str = _env("CVTOOL_LLM_MODE", "server")
    # 에이전트(MCP · API) 접속을 받을지. 토큰이 있어도 이게 꺼져 있으면 모두 거절한다.
    agent_api: bool = _env("CVTOOL_AGENT_API", "0").lower() in ("1", "true", "yes")


#: 관리자가 화면(운영 설정)에서 바꿀 수 있는 항목. 비밀값·주소는 넣지 않는다 (.env 로만).
#: (이름, 라벨, 종류, 설명) — 종류: bool · int · 고르기:값=라벨|값=라벨
운영항목 = (
    ("llm_mode", "AI 분석을 누가 하나", "고르기:server=서버 LLM|agent=사내 코워크 (에이전트)",
     "사내 코워크를 고르면 시스템이 LLM 을 부르지 않습니다. CV 분석 · 과제 매칭이 «대기» 로 쌓이고, "
     "코워크에게 «대기 중인 것 처리해줘» 라고 시키면 코워크가 가져가 처리합니다."),
    ("agent_api", "에이전트 접속 받기 (MCP · API)", "bool",
     "끄면 토큰이 있어도 모든 에이전트 요청을 거절합니다."),
    ("match_auto", "CV 분석 뒤 과제 매칭 자동", "bool",
     "서버 LLM 일 때만 시스템이 바로 맞춰 봅니다. 사내 코워크일 때는 매칭 대기로 쌓입니다."),
    ("mail_dry_run", "메일 연습 모드 (실제로 안 보냄)", "bool",
     "켜져 있으면 발송을 눌러도 기록만 남고 메일은 나가지 않습니다."),
    ("store_cv_text", "CV 원문 텍스트 보관", "bool",
     "켜면 재업로드 없이 재분석할 수 있지만 CV 전문이 DB 에 남습니다."),
    ("llm_thinking", "서버 LLM: 생각(thinking) 켜기", "bool",
     "끄면 모델이 생각 없이 바로 답해 훨씬 빠릅니다 (Qwen3 계열 · vLLM). 정확도는 직접 확인하세요."),
    ("two_stage", "서버 LLM: 2단계 추출 (먼저 읽고 정리)", "bool", "서버 LLM 일 때만 씁니다."),
    ("oneshot", "서버 LLM: 한 번에 추출", "bool", "서버 LLM 일 때만 씁니다. 빠르지만 정확도는 직접 확인하세요."),
    ("match_show", "지원자 상세에 보여 줄 상위 과제 수", "int", ""),
)


class 운영설정:
    """`Settings` 에 관리자가 화면에서 바꾼 값을 덮어 보여 준다.

    `.env` 값이 기본이고, 운영 설정 파일(데이터 폴더의 운영설정.json)에 있는 항목만
    덮어쓴다. 코드는 지금처럼 `settings.match_auto` 로 읽으면 된다 — 바꾸면 다음
    읽을 때부터 바로 반영된다 (다시 켤 필요 없음).
    """

    # dataclasses.replace(settings, …) 가 그대로 되게 (테스트가 많이 쓴다)
    __dataclass_fields__ = Settings.__dataclass_fields__
    __dataclass_params__ = Settings.__dataclass_params__

    def __init__(self, 기본: Settings | None = None, **값) -> None:
        if 기본 is None:
            기본 = Settings(**값)
        object.__setattr__(self, "기본값", 기본)
        object.__setattr__(self, "_덮어씀", {})
        object.__setattr__(self, "_파일", None)

    def __getattr__(self, 이름: str):
        덮어씀 = object.__getattribute__(self, "_덮어씀")
        if 이름 in 덮어씀:
            return 덮어씀[이름]
        return getattr(object.__getattribute__(self, "기본값"), 이름)

    def __setattr__(self, 이름, 값):
        raise AttributeError("설정은 운영설정.바꾸기() 로만 바꿉니다")

    # -- 운영 설정 파일 --------------------------------------------------------
    def 파일쓰기(self, 경로) -> None:
        """운영 설정 파일 위치를 정하고 읽는다 (웹 앱이 뜰 때 한 번)."""
        from pathlib import Path

        object.__setattr__(self, "_파일", Path(경로))
        self._덮어씀.clear()
        try:
            import json

            저장 = json.loads(Path(경로).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            저장 = {}
        for 이름, 값 in (저장 or {}).items():
            try:
                self._덮어씀[이름] = _맞추기(이름, 값)
            except (KeyError, ValueError):
                continue

    def 바꾸기(self, 값들: dict) -> dict:
        """{이름: 새 값} 을 덮어쓰고 파일에 남긴다. 실제로 바뀐 것 {이름: (전, 후)}."""
        바뀐 = {}
        for 이름, 값 in 값들.items():
            새 = _맞추기(이름, 값)
            전 = getattr(self, 이름)
            if 새 != 전:
                바뀐[이름] = (전, 새)
            self._덮어씀[이름] = 새
        if self._파일 is not None:
            import json

            self._파일.parent.mkdir(parents=True, exist_ok=True)
            self._파일.write_text(json.dumps(self._덮어씀, ensure_ascii=False, indent=1),
                                 encoding="utf-8")
        return 바뀐

    def 화면에서_바꿈(self, 이름: str) -> bool:
        return 이름 in self._덮어씀


def _맞추기(이름: str, 값):
    """운영 항목 값을 제 모양으로. 모르는 항목이면 KeyError, 값이 틀리면 ValueError."""
    종류 = next(k for n, _l, k, _d in 운영항목 if n == 이름)
    if 종류 == "bool":
        if isinstance(값, bool):
            return 값
        return str(값).strip().lower() in ("1", "true", "yes", "on", "켜짐")
    if 종류 == "int":
        return max(0, int(str(값).strip()))
    고를것 = [x.split("=", 1)[0] for x in 종류.split(":", 1)[1].split("|")]
    if str(값) not in 고를것:
        raise ValueError(f"{이름}: {값}")
    return str(값)


settings = 운영설정(Settings())
