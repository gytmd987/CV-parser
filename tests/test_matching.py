"""연구 과제 매칭 — 과제 파일 읽기, 후보 좁히기, 점수·사유."""

from __future__ import annotations

import json

import pytest

from cvtool import projects as pm
from cvtool.matching import Match, candidate_profile, match, shortlist
from cvtool.projects import Project, ProjectsError, load, parse, resolve_path
from cvtool.schemas import CVRecord


# --- 경로 --------------------------------------------------------------------
def test_relative_path_is_resolved_from_the_repo_folder():
    """`cd ../과제정보` 로 가는 곳이면 `../과제정보/과제.json` 이라고 적으면 된다."""
    나온것 = resolve_path("../과제정보/과제.json")
    assert 나온것 == (pm.REPO_ROOT.parent / "과제정보" / "과제.json").resolve()
    assert 나온것.is_absolute()


def test_absolute_path_is_left_alone():
    assert str(resolve_path("/opt/data/과제.json")) == "/opt/data/과제.json"


def test_home_shortcut_works():
    assert "~" not in str(resolve_path("~/과제.json"))


def test_quotes_and_spaces_are_trimmed():
    assert resolve_path('  "/opt/a.json" ') == resolve_path("/opt/a.json")


def test_empty_path_is_none():
    assert resolve_path("") is None


# --- 파일 모양을 가리지 않는다 ---------------------------------------------------
def test_plain_list():
    ps = parse([{"과제명": "가"}, {"name": "나"}])
    assert [p.이름 for p in ps] == ["가", "나"]


def test_wrapped_list():
    assert [p.이름 for p in parse({"projects": [{"title": "가"}]})] == ["가"]


def test_dict_keyed_by_project_number():
    ps = parse({"P-001": {"과제명": "가"}, "P-002": {"과제명": "나"}})
    assert [(p.번호, p.이름) for p in ps] == [("P-001", "가"), ("P-002", "나")]


def test_single_project_object():
    assert [p.이름 for p in parse({"과제명": "가", "설명": "나"})] == ["가"]


def test_unknown_fields_are_kept_in_the_description():
    """모르는 필드를 버리면 매칭에 쓸 정보가 사라진다."""
    p = parse([{"과제명": "가", "예산": "10억", "기간": {"시작": "2024"}}])[0]
    assert "예산: 10억" in p.설명 and "시작: 2024" in p.설명


def test_keywords_from_a_list_or_a_string():
    assert parse([{"과제명": "가", "키워드": ["A", "B"]}])[0].키워드 == ["A", "B"]
    assert parse([{"과제명": "가", "keywords": "A, B;C"}])[0].키워드 == ["A", "B", "C"]


def test_entries_without_a_name_are_skipped():
    assert parse([{"설명": "이름이 없다"}, {"과제명": "가"}]) == parse([{"과제명": "가"}]) \
        or [p.이름 for p in parse([{"설명": "x"}, {"과제명": "가"}])] == ["가"]


def test_duplicate_numbers_get_distinct_keys():
    ps = parse([{"과제명": "가", "id": "P1"}, {"과제명": "나", "id": "P1"}])
    assert len({p.키 for p in ps}) == 2


def test_summary_has_everything_the_llm_needs():
    p = parse([{"과제명": "차세대 공정", "id": "P-1", "키워드": ["반도체"],
                "담당": "김박사", "설명": "식각 공정 연구"}])[0]
    글 = p.요약()
    for 조각 in ("차세대 공정", "P-1", "반도체", "김박사", "식각"):
        assert 조각 in 글


# --- 파일 문제는 이유를 말해준다 ---------------------------------------------------
def test_missing_file_says_where(tmp_path):
    with pytest.raises(ProjectsError) as exc:
        load(tmp_path / "없다.json")
    assert "없습니다" in str(exc.value) and "없다.json" in str(exc.value)


def test_broken_json_says_which_line(tmp_path):
    f = tmp_path / "과제.json"
    f.write_text('{\n  "과제명": "가",\n', encoding="utf-8")
    with pytest.raises(ProjectsError) as exc:
        load(f)
    assert "JSON 형식이 아닙니다" in str(exc.value)


def test_file_with_no_recognisable_projects(tmp_path):
    f = tmp_path / "과제.json"
    f.write_text('[{"설명": "이름이 없다"}]', encoding="utf-8")
    with pytest.raises(ProjectsError) as exc:
        load(f)
    assert "못 읽었습니다" in str(exc.value)


def test_empty_path_says_what_to_set():
    with pytest.raises(ProjectsError) as exc:
        load("")
    assert "CVTOOL_PROJECTS_JSON" in str(exc.value)


def test_loads_a_real_file(tmp_path):
    f = tmp_path / "과제.json"
    f.write_text(json.dumps([{"과제명": "가"}, {"과제명": "나"}], ensure_ascii=False),
                 encoding="utf-8")
    assert len(load(f)) == 2


# --- 지원자 요약 ---------------------------------------------------------------
def test_profile_has_research_facts():
    rec = CVRecord(지원자_ID="CV-1", 한글_이름="홍길동", 전화번호="010-1234-5678",
                   박사_학교="한국대학교", 박사_전공="전기공학",
                   연구분야_키워드="반도체 식각", 경력_요약="가나다반도체 공정개발 3년")
    글 = candidate_profile(rec)
    assert "전기공학" in 글 and "반도체 식각" in 글 and "가나다반도체" in 글


def test_profile_leaves_out_personal_details():
    """이름·연락처는 판단에 필요 없다. 넣으면 엉뚱한 판단의 여지만 생긴다."""
    rec = CVRecord(지원자_ID="CV-1", 한글_이름="홍길동", 전화번호="010-1234-5678",
                   이메일="hong@x.com", 생년월일="19900101", 박사_전공="전기공학")
    글 = candidate_profile(rec)
    for 개인정보 in ("홍길동", "010-1234-5678", "hong@x.com", "19900101"):
        assert 개인정보 not in 글


# --- 후보 좁히기 ---------------------------------------------------------------
def _과제(n: int) -> list[Project]:
    return parse([{"과제명": f"과제{i}", "설명": f"내용{i}"} for i in range(n)])


def test_few_projects_are_all_kept():
    목록 = _과제(3)
    assert shortlist("아무거나", 목록, top=8) == 목록


def test_many_projects_are_narrowed():
    assert len(shortlist("아무거나", _과제(30), top=5)) == 5


def test_embedding_failure_falls_back_to_word_overlap():
    """임베딩이 죽어도 매칭은 되어야 한다."""
    class 고장난것:
        def embed(self, texts):
            raise RuntimeError("TEI 죽음")

    목록 = parse([{"과제명": f"과제{i}", "설명": "반도체 식각" if i == 7 else "생물"}
                for i in range(20)])
    좁힌것 = shortlist("반도체 식각 공정", 목록, top=3, embed_client=고장난것())
    assert len(좁힌것) == 3
    assert any("과제7" == p.이름 for p in 좁힌것)


def test_embedding_is_used_when_it_works():
    class 가짜:
        def embed(self, texts):
            # 첫 번째(지원자)와 똑같은 벡터를 세 번째 과제에만 준다
            return [[1.0, 0.0]] + [[0.0, 1.0]] * (len(texts) - 1 - 1) + [[1.0, 0.0]]

    목록 = _과제(12)
    좁힌것 = shortlist("지원자", 목록, top=2, embed_client=가짜())
    assert 목록[-1] in 좁힌것


# --- 점수 · 사유 ---------------------------------------------------------------
class 가짜LLM:
    def __init__(self, 답):
        self.답 = 답
        self.받은프롬프트 = None

    def chat_json(self, messages, schema, **kw):
        self.받은프롬프트 = messages
        return self.답

    def close(self):
        pass


def test_match_returns_scores_and_reasons():
    목록 = parse([{"과제명": "차세대 공정", "id": "P-1"},
                {"과제명": "신소재", "id": "P-2"}])
    llm = 가짜LLM({"결과": [
        {"과제키": "P-2", "점수": 40, "사유": "겹치는 부분이 적습니다."},
        {"과제키": "P-1", "점수": 85, "사유": "식각 공정 경험이 그대로 맞습니다.",
         "근거": ["반도체 식각"]},
    ]})
    나온것 = match("반도체 식각", 목록, client=llm)
    assert [m.과제키 for m in 나온것] == ["P-1", "P-2"]      # 점수 높은 순
    assert 나온것[0].과제명 == "차세대 공정"
    assert 나온것[0].근거 == ["반도체 식각"]
    assert 나온것[0].등급 == "매우 적합"


def test_invented_projects_are_dropped():
    """목록에 없는 과제를 지어내면 버린다."""
    목록 = parse([{"과제명": "가", "id": "P-1"}])
    llm = 가짜LLM({"결과": [
        {"과제키": "P-1", "점수": 70, "사유": "가"},
        {"과제키": "없는과제", "점수": 99, "사유": "지어냄"},
    ]})
    assert [m.과제키 for m in match("아무거나", 목록, client=llm)] == ["P-1"]


def test_scores_are_clamped():
    목록 = parse([{"과제명": "가", "id": "P-1"}])
    llm = 가짜LLM({"결과": [{"과제키": "P-1", "점수": 999, "사유": "가"}]})
    assert match("x", 목록, client=llm)[0].점수 == 100


def test_prompt_forbids_making_things_up():
    목록 = parse([{"과제명": "가", "id": "P-1"}])
    llm = 가짜LLM({"결과": []})
    match("지원자 정보", 목록, client=llm)
    지시 = llm.받은프롬프트[0]["content"]
    assert "지어내지 마라" in 지시
    assert "P-1" in llm.받은프롬프트[1]["content"]


def test_no_projects_means_no_matches():
    assert match("x", [], client=가짜LLM({"결과": []})) == []


def test_empty_profile_is_refused():
    from cvtool.clients.llm import LLMError

    with pytest.raises(LLMError):
        match("   ", parse([{"과제명": "가"}]), client=가짜LLM({"결과": []}))


def test_grades_read_in_korean():
    assert Match("k", "n", 85, "").등급 == "매우 적합"
    assert Match("k", "n", 65, "").등급 == "적합"
    assert Match("k", "n", 45, "").등급 == "보통"
    assert Match("k", "n", 10, "").등급 == "낮음"


# --- 저장 --------------------------------------------------------------------
def test_matches_are_stored_and_ordered(tmp_path):
    from cvtool.store import CandidateStore

    s = CandidateStore(tmp_path / "c.db")
    s.save(CVRecord(지원자_ID="CV-1"))
    s.save_matches("CV-1", [Match("P-1", "가", 88, "잘 맞음", ["근거1"]),
                            Match("P-2", "나", 40, "덜 맞음")])
    나온것 = s.matches("CV-1")
    assert [m["순위"] for m in 나온것] == [1, 2]
    assert 나온것[0]["근거"] == ["근거1"]
    assert s.top_matches()["CV-1"]["과제명"] == "가"


def test_rematching_replaces_the_old_result(tmp_path):
    """과제 목록이 바뀌면 옛 결과는 뜻이 없다."""
    from cvtool.store import CandidateStore

    s = CandidateStore(tmp_path / "c.db")
    s.save(CVRecord(지원자_ID="CV-1"))
    s.save_matches("CV-1", [Match("P-1", "가", 88, "")])
    s.save_matches("CV-1", [Match("P-9", "새것", 50, "")])
    assert [m["과제키"] for m in s.matches("CV-1")] == ["P-9"]


def test_matches_go_away_with_the_candidate(tmp_path):
    from cvtool.store import CandidateStore

    s = CandidateStore(tmp_path / "c.db")
    s.save(CVRecord(지원자_ID="CV-1"))
    s.save_matches("CV-1", [Match("P-1", "가", 88, "")])
    s.delete("CV-1")
    assert s.matches("CV-1") == []
    assert s.matched_count() == 0


# --- 화면이 실제로 쓰이는지 -------------------------------------------------------
def test_no_shadowed_definitions_in_the_web_module():
    """같은 이름을 두 번 정의하면 뒤엣것이 이겨서, 고친 화면이 안 보인다.

    실제로 낡은 사본이 지원자 상세 화면을 가려서 메일·과제 카드가 안 나왔다.
    """
    import collections
    import pathlib
    import re

    소스 = pathlib.Path("cvtool/web/app.py").read_text(encoding="utf-8")
    센것 = collections.Counter(
        m.group(1) for m in re.finditer(r"^def (\w+)\(", 소스, re.MULTILINE)
    )
    겹친것 = {이름: 수 for 이름, 수 in 센것.items() if 수 > 1}
    assert not 겹친것, f"두 번 정의된 함수: {겹친것}"


# --- 사내 과제 파일 모양 (dep_name / project_name / core_tech ...) ----------------
사내과제 = {
    "dep_name": "공정개발팀",
    "project_name": "차세대 반도체 식각 공정 개발",
    "core_tech": "플라즈마 진단",
    "deliverable": "공정 레시피",
    "challenge": "고종횡비 균일도",
    "background": "소자 미세화",
    "milestones": ["2024 1차"],
    "expected_impact": "수율 3%p",
    "keywords_kr": ["반도체", "식각"],
    "keywords_en": ["semiconductor", "etching"],
    "작성자": "김철수",
    "문서버전": "v3.2",
}


def test_company_field_names_are_understood():
    p = parse([사내과제])[0]
    assert p.이름 == "차세대 반도체 식각 공정 개발"
    assert p.담당 == "공정개발팀"


def test_korean_and_english_keywords_are_merged():
    """keywords_kr 과 keywords_en 이 따로 있으면 둘 다 쓴다."""
    assert parse([사내과제])[0].키워드 == ["반도체", "식각", "semiconductor", "etching"]


def test_known_fields_get_korean_labels_in_the_summary():
    """LLM 이 읽을 글이라 core_tech 보다 '핵심 기술' 이 낫다."""
    글 = parse([사내과제])[0].요약()
    for 라벨 in ("핵심 기술", "산출물", "기술적 난제", "배경", "기대 효과"):
        assert 라벨 in 글


# --- 다듬기 --------------------------------------------------------------------
def _사내파일(tmp_path, 개수: int = 3):
    from cvtool.projects import raw_items, read_json

    데이터 = []
    for i in range(개수):
        d = dict(사내과제)
        d["project_name"] = f"과제{i}"
        d["코드"] = f"P-{i}"
        데이터.append(d)
    f = tmp_path / "raw.json"
    f.write_text(json.dumps(데이터, ensure_ascii=False), encoding="utf-8")
    return f, raw_items(read_json(f))


def test_field_stats_lists_every_field_with_a_sample(tmp_path):
    from cvtool.projects import field_stats

    _f, 항목 = _사내파일(tmp_path)
    통계 = {x.이름: x for x in field_stats(항목)}
    assert 통계["core_tech"].라벨 == "핵심 기술"
    assert 통계["core_tech"].채운수 == 3 and 통계["core_tech"].비율 == 100
    assert 통계["core_tech"].예시 == "플라즈마 진단"
    assert 통계["project_name"].필수 is True       # 이름은 뺄 수 없다
    assert 통계["작성자"].필수 is False


def test_field_stats_counts_only_filled_values(tmp_path):
    from cvtool.projects import field_stats, raw_items

    항목 = raw_items([{"project_name": "가", "메모": "있음"},
                    {"project_name": "나", "메모": ""}])
    통계 = {x.이름: x for x in field_stats(항목)}
    assert (통계["메모"].채운수, 통계["메모"].전체수) == (1, 2)


def test_curate_keeps_only_what_was_chosen(tmp_path):
    from cvtool.projects import curate, item_key

    _f, 항목 = _사내파일(tmp_path)
    나온것 = curate(항목, {item_key(*항목[0])}, {"dep_name", "core_tech"})
    assert len(나온것) == 1
    assert set(나온것[0]) == {"dep_name", "core_tech", "project_name"}   # 이름은 항상
    assert "작성자" not in 나온것[0]


def test_curate_keeps_the_name_even_if_unchecked(tmp_path):
    from cvtool.projects import curate

    _f, 항목 = _사내파일(tmp_path, 1)
    assert "project_name" in curate(항목, None, {"dep_name"})[0]


def test_curate_drops_unselected_projects(tmp_path):
    from cvtool.projects import curate

    from cvtool.projects import item_key

    _f, 항목 = _사내파일(tmp_path, 3)
    고른것 = {item_key(*항목[0]), item_key(*항목[2])}
    assert len(curate(항목, 고른것, None)) == 2


def test_curated_file_reads_back_as_projects(tmp_path):
    from cvtool.projects import curate, curated_meta, save_curated

    from cvtool.projects import item_key

    _f, 항목 = _사내파일(tmp_path, 2)
    고른것 = curate(항목, {item_key(*항목[0])},
                 {"dep_name", "core_tech", "keywords_kr"})
    저장 = save_curated(tmp_path / "curated.json", 고른것, 출처="원본", 만든이="admin")

    다시 = load(저장)
    assert [p.이름 for p in 다시] == ["과제0"]
    assert 다시[0].담당 == "공정개발팀"
    assert "작성자" not in 다시[0].요약()          # 뺀 정보는 LLM 에 안 간다

    메타 = curated_meta(저장)
    assert 메타["과제수"] == 1 and 메타["만든이"] == "admin"
    assert "core_tech" in 메타["필드"]


def test_curated_meta_is_empty_when_there_is_no_file(tmp_path):
    from cvtool.projects import curated_meta

    assert curated_meta(tmp_path / "없다.json") == {}


def test_raw_items_handles_every_shape():
    from cvtool.projects import raw_items

    assert len(raw_items([{"a": 1}, {"b": 2}])) == 2
    assert len(raw_items({"projects": [{"a": 1}]})) == 1
    assert raw_items({"P-1": {"a": 1}})[0][0] == "P-1"
    assert len(raw_items({"과제명": "가"})) == 1


def test_dict_key_survives_curation(tmp_path):
    """번호가 사전 키로만 있던 경우에도 번호를 잃지 않는다."""
    from cvtool.projects import curate, raw_items

    항목 = raw_items({"P-9": {"project_name": "가", "core_tech": "나"}})
    나온것 = curate(항목, {"P-9"}, {"core_tech"})
    assert 나온것[0]["과제번호"] == "P-9"


def test_the_key_is_computed_the_same_way_everywhere(tmp_path):
    """화면에서 고른 키와 저장할 때 쓰는 키가 다르면 고른 과제가 빠진다."""
    from cvtool.projects import curate, item_key, raw_items

    항목 = raw_items([{"project_name": "가", "코드": "P-1"},
                    {"project_name": "나"}])
    키들 = {item_key(k, v) for k, v in 항목}
    assert 키들 == {"P-1", "나"}
    assert len(curate(항목, 키들, None)) == 2
