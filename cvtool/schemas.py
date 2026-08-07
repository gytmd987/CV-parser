"""CV 구조화 스키마.

- CV_JSON_SCHEMA : vLLM `guided_json` / OpenAI `response_format` 에 그대로 넣는 JSON 스키마.
- Pydantic 모델   : LLM 출력 검증 및 애플리케이션 내부 타입.

개인정보 주의: 연락처/이메일은 추출은 하되(원천이므로), 벡터 DB payload 에는 넣지 않습니다.
payload 에는 식별자만, 개인정보는 Postgres 에 둡니다. (매칭 슬라이스에서 강제)
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# 요청서에 제시된 스키마를 그대로 사용 (한글 키). 연락처/이메일만 선택 항목으로 보강.
CV_JSON_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "이름": {"type": "string"},
        "이메일": {"type": "string"},
        "연락처": {"type": "string"},
        "총_경력_개월": {"type": "integer"},
        "최종학력": {
            "type": "string",
            "enum": ["고졸", "전문학사", "학사", "석사", "박사"],
        },
        "보유_스킬": {"type": "array", "items": {"type": "string"}},
        "경력": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "회사": {"type": "string"},
                    "직무": {"type": "string"},
                    "시작": {"type": "string"},
                    "종료": {"type": "string"},
                },
            },
        },
    },
    "required": ["이름", "총_경력_개월", "보유_스킬"],
}


class Career(BaseModel):
    회사: str = ""
    직무: str = ""
    시작: str = ""
    종료: str = ""


class CVRecord(BaseModel):
    이름: str
    총_경력_개월: int
    보유_스킬: list[str] = Field(default_factory=list)
    최종학력: Optional[str] = None
    이메일: Optional[str] = None
    연락처: Optional[str] = None
    경력: list[Career] = Field(default_factory=list)

    def pii_fields(self) -> dict:
        """Postgres 로만 보내야 하는 개인식별정보."""
        return {"이름": self.이름, "이메일": self.이메일, "연락처": self.연락처}
