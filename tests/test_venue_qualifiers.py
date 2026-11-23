"""학회·저널 묶기 키에 성격 말(Findings · Workshop …)을 남긴다."""

from __future__ import annotations

import sqlite3

import pytest

from cvtool.names import NameRegistry, normalize


@pytest.mark.parametrize("표기, 키", [
    ("EMNLP (Findings)", "emnlpfindings"),
    ("Findings of EMNLP 2023", "emnlpfindings"),
    ("EMNLP Findings", "emnlpfindings"),
    ("EMNLP", "emnlp"),
    ("Proc. of EMNLP 2023", "emnlp"),
    ("EMNLP (2023)", "emnlp"),
    ("ACL Student Research Workshop", "aclresearchstudentworkshop"),
    ("NeurIPS Workshops", "neuripsworkshop"),
])
def test_성격_말은_어디_있든_키_끝에(표기, 키):
    assert normalize(표기, "학회") == 키


def test_소속_전공은_그대로():
    assert normalize("포항공과대학교(POSTECH)", "소속") == "포항공과대학교"


def test_Findings_는_본_학회_이름_등급을_물려받지_않는다(tmp_path):
    r = NameRegistry(tmp_path / "n.db")
    본 = r.observe("학회", "EMNLP")
    r.confirm(본.id, "admin")
    r.우수_저장("EMNLP", "최우수")
    f = r.observe("학회", "EMNLP (Findings)")
    assert f.표시명 == "EMNLP (Findings)" and not f.확인 and f.등급 == "미분류"
    g = r.observe("학회", "Findings of EMNLP 2023")
    assert g.표시명 == "EMNLP (Findings)"                    # Findings 끼리는 묶인다
    r.우수_저장("EMNLP Findings", "우수")
    assert r.get(f.id).등급 == "우수" and r.get(본.id).등급 == "최우수"


def test_예전_키로_잘못_묶인_표기는_다시_보게_한다(tmp_path):
    path = tmp_path / "n.db"
    r = NameRegistry(path)
    본 = r.observe("학회", "EMNLP")
    r.우수_저장("EMNLP", "최우수")
    r.close()
    # 예전 규칙: 괄호를 지워 EMNLP 그룹에 들어가 있던 Findings 표기 (사람이 확인까지 함)
    conn = sqlite3.connect(str(path))
    conn.execute("INSERT INTO names (종류,원표기,정규화키,표시명,발견횟수,최초등록,확인자,확인일시)"
                 " VALUES ('학회·저널','EMNLP (Findings)','emnlp','EMNLP',3,'','admin','2026-11-01 10:00')")
    conn.execute("DELETE FROM registry_meta WHERE 키='학회키_성격말'")
    conn.commit()
    conn.close()

    r = NameRegistry(path)
    f = r.lookup("학회", "EMNLP (Findings)")
    assert f.정규화키 == "emnlpfindings" and not f.확인          # 다시 볼 것으로
    assert f.표시명 == "EMNLP"                                 # 이름은 함부로 안 옮긴다
    r.merge([f.id], "EMNLP Findings")                         # 사람이 옮기면
    assert r.get(f.id).등급 == "미분류"                         # EMNLP 등급이 따라오지 않는다
    assert r.lookup("학회", "Findings of EMNLP 2024").표시명 == "EMNLP Findings"
