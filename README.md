# cvtool — 지원자 CV 분석 툴

사내 온프레미스(폐쇄망) 워크스테이션용 CV 분석 도구입니다.
**기존 인사 문서 RAG 시스템과 완전히 분리**해서 운영합니다.

## 설계 원칙 (요청서 기준)

- **모델을 새로 올리지 않는다.** GPU 여유가 ~20GB뿐이라 모델을 또 올리면 인사 RAG 가 OOM 으로
  죽습니다. 이미 떠 있는 로컬 서비스를 OpenAI 호환 API 로 **호출만** 합니다.
  | 서비스 | 주소 | 모델 | 용도 |
  |---|---|---|---|
  | vLLM | `http://localhost:8000/v1` | `thinkingcap` | 텍스트 생성 / 구조화 추출 |
  | TEI 임베딩 | `http://localhost:8081` | KURE-v1 (1024차원) | JD↔CV 유사도 |
  | TEI 리랭커 | `http://localhost:8082` | bge-reranker-v2-m3 | 후보 순위 정렬 |
- **저장소 분리.** 기존 Qdrant(6333)·Postgres(5432)는 인사 문서가 들어 있고 컬렉션 단위 권한이
  없어 붙기만 하면 전문이 노출됩니다. **별도 포트(5433 / 6335)** 로 새로 띄웁니다.
- **개인정보 보호.**
  - 벡터 DB payload 에는 **식별자만**, 이름·연락처 등 개인정보는 **Postgres 에만** 둡니다
    (`CVRecord.pii_fields()` 참고).
  - **보관 기간**을 정하고 지난 건 삭제합니다 (채용 종료 후 N개월, 기본 6). 판정 로직은
    `cvtool/retention.py` 에 순수 함수로 구현되어 있고 테스트됩니다.
  - 채용 담당자만 접근 (접근 통제는 서비스 슬라이스에서 추가).
  - 외부 API 호출 없음(폐쇄망, 전부 로컬).
- **시간대.** 서버 시계는 UTC 입니다. `datetime.now()` 를 그냥 쓰면 9시간 어긋납니다.
  항상 `cvtool.timeutil.now_kst()` 등으로 **Asia/Seoul 을 명시**합니다.

## ⚠️ 이 저장소가 만들어진 환경에 대한 정직한 고지

이 코드는 클라우드 개발 컨테이너에서 작성/테스트되었습니다. 그 환경에는 로컬 LLM/TEI,
`/opt/data-gov`, docker, GPU 가 **없습니다.** 따라서:

- **실제 로컬 LLM 대상 end-to-end 검증은 아직 못 했습니다.** 서비스가 떠 있는 온프레미스
  서버에서 `cvtool health` → `cvtool extract` 로 최종 확인해야 합니다.
- 대신 **LLM 을 목킹한 오프라인 테스트**(`tests/`)로 파이프라인 로직(스키마 강제, guided_json
  요청, response_format 폴백, 출력 검증, 파서, 타임존/리텐션)은 검증했습니다. (`pytest` 12개 통과)

## 폐쇄망 설치 주의

`pip install` 이 실패할 수 있으니, 아래 패키지가 서버에 **이미 있는지 먼저 확인**하세요.
없으면 알려 주시면 반입 방법을 함께 정하겠습니다.

- 핵심(추출): `httpx`, `pydantic` — 이것만 있으면 CV 구조화 추출이 동작합니다.
- 선택: `pypdf`(.pdf), `python-docx`(.docx) — 해당 형식을 쓸 때만 필요 (lazy import).
- 다음 슬라이스: `psycopg`, `qdrant-client`.

`.txt` CV 는 아무 의존성 없이 바로 됩니다.

## 사용법

```bash
# 저장소(별도 포트) 기동 — 비밀번호는 .env 로
cp .env.example .env && $EDITOR .env      # CVTOOL_PG_PASSWORD 설정
docker compose up -d

# 로컬 서비스 연결 확인
cvtool health

# CV 한 장 구조화 추출 (첫 슬라이스의 핵심)
cvtool extract 이력서.pdf
cvtool extract 이력서.docx
cvtool extract --text "홍길동 ... 이력서 전문 ..."
```

`extract` 는 `guided_json` 으로 JSON 스키마(`cvtool/schemas.py:CV_JSON_SCHEMA`)를 강제하고,
서버가 거부하면 OpenAI 표준 `response_format` 으로 자동 폴백합니다.

## 개발/테스트

```bash
pip install -e ".[dev]"   # 폐쇄망이면 httpx/pydantic/pytest 사전 확인
pytest -q
```

## 구조

```
cvtool/
  config.py            설정 (환경변수 override, 기본값=요청서 서버 기준)
  timeutil.py          KST 명시 시간 유틸
  schemas.py           CV_JSON_SCHEMA (guided_json) + CVRecord(pydantic)
  extract.py           텍스트/파일 -> 구조화 추출 (핵심)
  retention.py         보관기간 만료 판정 (순수 로직, 테스트됨)
  cli.py               `cvtool extract` / `cvtool health`
  ingestion/parsers.py .txt/.pdf/.docx 텍스트 추출
  clients/
    llm.py             vLLM: guided_json + response_format 폴백
    embedding.py       TEI 임베딩 (32개씩 배치)   ← 매칭 슬라이스에서 사용
    reranker.py        TEI 리랭커                  ← 매칭 슬라이스에서 사용
tests/                 오프라인(LLM 목킹) 테스트
docker-compose.yml     별도 Postgres(5433)/Qdrant(6335)
```

## 로드맵 (작게 쌓기)

- [x] **슬라이스 1 — CV 한 장 구조화 추출** (PDF/docx/txt → guided_json → 검증)
- [ ] 슬라이스 2 — Postgres 저장(개인정보) + Qdrant 인덱싱(식별자 payload)
- [ ] 슬라이스 3 — JD 입력 → 임베딩 유사도 + 리랭커로 적합도 정렬
- [ ] 슬라이스 4 — 강점/약점 요약, 면접 질문 생성, 중복 지원자 탐지
- [ ] 슬라이스 5 — 접근 통제 + 리텐션 자동 삭제 스케줄

## 참고 (수정 금지)

같은 서버 `/opt/data-gov` 인사 RAG 코드를 참고만 하세요. 특히
`app/clients/llm.py`(구조화 출력), `app/clients/embedding.py`(배치 32),
`app/ingestion/parsers/`(PDF/docx/이미지 OCR).
