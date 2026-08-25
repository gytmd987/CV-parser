"""여러 갈래가 한 sqlite 파일을 같이 쓸 때 쓰는 연결.

**왜 있는가.**

이 프로그램은 연결 하나(`sqlite3.connect(..., check_same_thread=False)`)를
여러 갈래가 같이 썼다. CV 분석은 뒤에서 도는 일꾼 갈래가 하고, 화면은
요청마다 새 갈래가 그린다. 그러면 서버 로그에 이런 게 찍힌다::

    sqlite3.InterfaceError: bad parameter or other API misuse

값을 잘못 넘겨서 나는 말처럼 보이지만 그게 아니다. sqlite 가 SQLITE_MISUSE
를 낼 때 쓰는 문구고, **한 연결을 두 갈래가 동시에 건드릴 때** 난다.
파이썬 sqlite3 는 같은 SQL 을 연결마다 미리 준비해 두고 재사용하는데, 그
준비물을 한 갈래가 쓰는 중에 다른 갈래가 값을 다시 꽂으면 이 오류가 된다.
그래서 파이썬 문서도 연결을 갈래끼리 나눠 쓸 거면 **직접 줄을 세우라**고
적어 두었다. 명칭 사전이 가장 자주 걸린 이유는 단순하다 — 표를 한 장
그릴 때마다 `_class_of` 가 논문 수만큼 돌아서, 겹칠 기회가 제일 많다.

**어떻게 막는가.** 두 가지다.

1. 문장 하나가 도는 동안 다른 갈래가 못 들어오게 자물쇠를 채운다.
2. `execute` 가 커서를 남기지 않는다. 결과를 **그 자리에서 다 읽어** 넘긴다.
   그래서 결과를 훑는 동안 안쪽에서 다른 질의를 넣어도 (`list_all` 이
   줄마다 `_class_of` 를 부르는 것처럼) 커서가 겹칠 일이 없다.

`atomic` 은 문장 여러 개를 한 덩어리로 묶는다. 자물쇠를 덩어리 내내 쥐고
있어서, 절반만 들어간 상태를 다른 갈래가 보거나 남의 절반짜리 작업을
commit 해 버리는 일이 없다.
"""

from __future__ import annotations

import functools
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence


class Rows:
    """이미 다 읽어 둔 결과. 커서인 척하지만 커서를 붙들지 않는다."""

    __slots__ = ("_rows", "_i", "lastrowid", "rowcount")

    def __init__(self, rows: list[sqlite3.Row], lastrowid: int | None,
                 rowcount: int) -> None:
        self._rows = rows
        self._i = 0
        self.lastrowid = lastrowid
        self.rowcount = rowcount

    def __iter__(self) -> Iterator[sqlite3.Row]:
        return iter(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def fetchone(self) -> sqlite3.Row | None:
        if self._i >= len(self._rows):
            return None
        self._i += 1
        return self._rows[self._i - 1]

    def fetchall(self) -> list[sqlite3.Row]:
        남은것 = self._rows[self._i:]
        self._i = len(self._rows)
        return 남은것


class Db:
    """자물쇠가 달린 sqlite 연결. `sqlite3.Connection` 자리에 그대로 들어간다."""

    def __init__(self, path: str | Path, *, wal: bool = True,
                 busy_timeout: int = 5000) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if wal:
            # 여러 사람이 동시에 써도 읽기가 막히지 않게 한다
            self.execute("PRAGMA journal_mode=WAL")
        self.execute(f"PRAGMA busy_timeout={int(busy_timeout)}")

    # -- 연결 흉내 -----------------------------------------------------------
    @property
    def row_factory(self):
        return self._conn.row_factory

    @row_factory.setter
    def row_factory(self, factory) -> None:
        with self._lock:
            self._conn.row_factory = factory

    @property
    def in_transaction(self) -> bool:
        return self._conn.in_transaction

    def execute(self, sql: str, args: Sequence[Any] = ()) -> Rows:
        with self._lock:
            cur = self._conn.execute(sql, args)
            try:
                rows = cur.fetchall()
                return Rows(rows, cur.lastrowid, cur.rowcount)
            finally:
                cur.close()

    def executescript(self, sql: str) -> None:
        with self._lock:
            self._conn.executescript(sql)

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- 덩어리로 묶기 --------------------------------------------------------
    def __enter__(self) -> "Db":
        self._lock.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self._lock.release()


def atomic(fn: Callable) -> Callable:
    """문장 여러 개를 한 덩어리로. `self._conn` 이 `Db` 여야 한다.

    쓰기가 여러 문장으로 나뉜 함수에 붙인다. 절반만 들어간 상태를 다른
    갈래가 보거나 commit 해 버리는 일을 막는다.
    """

    @functools.wraps(fn)
    def _묶어서(self, *args, **kwargs):
        with self._conn:
            return fn(self, *args, **kwargs)

    return _묶어서
