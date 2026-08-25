"""한 연결을 여러 갈래가 같이 쓸 때 — `sqlite3.InterfaceError` 를 막는 장치.

서버 로그에 이게 찍혔었다::

    sqlite3.InterfaceError: bad parameter or other API misuse
      ... schemas.py papers_view -> names.py lookup -> _row -> _class_of

값을 잘못 넘겨서 나는 오류가 아니라, **연결 하나를 두 갈래가 동시에**
건드려서 나는 오류다(SQLITE_MISUSE). CV 분석은 뒤에서 도는 일꾼 갈래가,
화면은 요청마다 새 갈래가 그리니 언제든 겹친다.

여기 있는 시험은 두 가지를 지킨다.
  1. 모든 저장소가 자물쇠 달린 연결(`dbconn.Db`)을 쓴다.
  2. 결과를 훑는 도중에 안쪽에서 다른 질의를 넣어도 커서가 겹치지 않는다.
"""

from __future__ import annotations

import ast
import pathlib
import sqlite3
import threading

import pytest

from cvtool import dbconn
from cvtool.dbconn import Db, Rows, atomic
from cvtool.names import NameRegistry
from cvtool.schemas import CVRecord, Paper
from cvtool.store import CandidateStore


@pytest.fixture
def db(tmp_path):
    d = Db(tmp_path / "t.db")
    d.executescript(
        "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, b TEXT);"
        "CREATE TABLE u (a TEXT, b TEXT);"
    )
    for i in range(20):
        d.execute("INSERT INTO t (a,b) VALUES (?,?)", (str(i), str(i)))
        d.execute("INSERT INTO u VALUES (?,?)", (str(i), str(i)))
    d.commit()
    return d


# --- 연결 흉내 ----------------------------------------------------------------
def test_execute_leaves_no_open_cursor(db):
    """결과를 훑는 도중에 다른 질의를 넣어도 되어야 한다.

    명칭 사전이 정확히 이 모양이다 — `list_all` 이 줄마다 `_class_of` 를 부른다.
    """
    본것 = []
    for r in db.execute("SELECT * FROM t ORDER BY id"):
        안쪽 = db.execute("SELECT b FROM u WHERE a=?", (r["a"],)).fetchone()
        본것.append(안쪽["b"])
    assert 본것 == [str(i) for i in range(20)]


def test_rows_behave_like_a_cursor(db):
    res = db.execute("SELECT * FROM t ORDER BY id LIMIT 3")
    assert len(res) == 3
    첫 = res.fetchone()
    assert 첫["a"] == "0"
    assert [r["a"] for r in res.fetchall()] == ["1", "2"]
    assert res.fetchone() is None


def test_lastrowid_and_rowcount_survive(db):
    cur = db.execute("INSERT INTO t (a,b) VALUES (?,?)", ("새", "값"))
    assert cur.lastrowid > 0
    바뀜 = db.execute("UPDATE t SET b='x' WHERE a=?", ("새",))
    assert 바뀜.rowcount == 1
    db.commit()


def test_row_factory_is_row_by_default(db):
    assert dict(db.execute("SELECT a FROM t LIMIT 1").fetchone()) == {"a": "0"}


def test_errors_release_the_lock(db):
    """질의가 틀렸다고 자물쇠를 쥔 채로 죽으면 서버 전체가 멈춘다."""
    with pytest.raises(sqlite3.OperationalError):
        db.execute("SELECT * FROM 없는표")
    assert db._lock.acquire(blocking=False)
    db._lock.release()
    assert len(db.execute("SELECT * FROM t")) == 20


# --- 덩어리로 묶기 --------------------------------------------------------------
def test_atomic_holds_the_lock_for_the_whole_group(db):
    """묶어 둔 동안에는 다른 갈래가 끼어들지 못해야 한다."""

    class 가짜:
        def __init__(self, conn):
            self._conn = conn

        @atomic
        def 두번(self, 끼어들수있나: list):
            끼어들수있나.append(다른갈래가_잡아보기(self._conn))

    끼어들수있나: list[bool] = []
    가짜(db).두번(끼어들수있나)
    assert 끼어들수있나 == [False]
    assert 다른갈래가_잡아보기(db) is True      # 끝나면 풀린다


def 다른갈래가_잡아보기(d: Db) -> bool:
    """다른 갈래에서 자물쇠를 잡을 수 있는지. (같은 갈래면 재진입돼서 무의미)"""
    결과: list[bool] = []

    def 해보기():
        잡힘 = d._lock.acquire(blocking=False)
        if 잡힘:
            d._lock.release()
        결과.append(잡힘)

    t = threading.Thread(target=해보기)
    t.start()
    t.join()
    return 결과[0]


# --- 실제 저장소 ---------------------------------------------------------------
def test_every_store_uses_the_locked_connection(tmp_path):
    """새 저장소를 만들 때 맨 연결로 되돌아가면 오류가 되살아난다."""
    from cvtool.audit import AuditLog
    from cvtool.auth import AuthStore
    from cvtool.dashboards import DashboardStore
    from cvtool.mailing import MailStore
    from cvtool.recruit import RecruitStore

    만들것 = [NameRegistry, CandidateStore, AuthStore, MailStore,
             RecruitStore, AuditLog, DashboardStore]
    for i, 클래스 in enumerate(만들것):
        저장소 = 클래스(tmp_path / f"{i}.db")
        assert isinstance(저장소._conn, Db), 클래스.__name__


def test_no_raw_connection_is_left_in_the_package():
    """`sqlite3.connect` 는 dbconn 한 곳에만 있어야 한다."""
    남은것 = []
    for p in pathlib.Path("cvtool").rglob("*.py"):
        if p.name == "dbconn.py":
            continue
        for node in ast.walk(ast.parse(p.read_text())):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "connect"):
                남은것.append(f"{p}:{node.lineno}")
    assert 남은것 == []


def test_the_name_registry_survives_a_crowd(tmp_path):
    """표를 그리는 갈래와 CV 를 분석하는 갈래가 같이 돌아도 터지지 않아야 한다."""
    reg = NameRegistry(tmp_path / "n.db")
    for i in range(30):
        reg.observe("학회·저널", f"학회{i}", 국내해외="해외", 유형="저널")

    논문들 = [Paper(제출처=f"학회{i}", 유형="저널", 저자순위="1저자") for i in range(30)]
    rec = CVRecord(지원자_ID="A", 한글_이름="홍길동", 논문=논문들)
    오류: list[str] = []

    def 그리기():
        try:
            for _ in range(40):
                rec.papers_view(reg)
                reg.list_all()
                reg.unclassified_count()
        except Exception as e:                     # noqa: BLE001 — 무엇이든 실패다
            오류.append(repr(e))

    def 분석하기():
        try:
            for i in range(40):
                reg.observe("학회·저널", f"새학회{i}", 국내해외="국내")
                reg.classify(reg.observe("학회·저널", f"학회{i % 30}").id, 등급="우수")
        except Exception as e:                     # noqa: BLE001
            오류.append(repr(e))

    갈래들 = [threading.Thread(target=그리기) for _ in range(4)]
    갈래들 += [threading.Thread(target=분석하기) for _ in range(2)]
    for t in 갈래들:
        t.start()
    for t in 갈래들:
        t.join()
    assert 오류 == []


def test_the_candidate_store_survives_a_crowd(tmp_path):
    """지원자 저장소도 같은 문제를 안고 있었다."""
    store = CandidateStore(tmp_path / "c.db")
    for i in range(10):
        store.save(CVRecord(지원자_ID=f"A{i}", 한글_이름=f"이름{i}"))
    오류: list[str] = []

    def 읽기():
        try:
            for _ in range(50):
                store.list_all()
                store.get("A3")
        except Exception as e:                     # noqa: BLE001
            오류.append(repr(e))

    def 쓰기():
        try:
            for i in range(50):
                store.save(CVRecord(지원자_ID=f"B{i}", 한글_이름=f"새{i}"))
        except Exception as e:                     # noqa: BLE001
            오류.append(repr(e))

    갈래들 = [threading.Thread(target=읽기) for _ in range(4)]
    갈래들 += [threading.Thread(target=쓰기) for _ in range(2)]
    for t in 갈래들:
        t.start()
    for t in 갈래들:
        t.join()
    assert 오류 == []
    assert len(store.list_all()) == 60
