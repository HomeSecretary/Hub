"""SQLite 하나에 Nonce 기록, 인증 실패 기록, 승인 대기열, 모듈 이벤트를 둔다.

uvicorn 워커는 1개로 운영한다. 개인 서버라 충분하고, 상태를 한 파일에 모을 수 있다.
"""
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS nonces (
    access_key TEXT NOT NULL,
    nonce      TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    PRIMARY KEY (access_key, nonce)
);
CREATE TABLE IF NOT EXISTS auth_failures (
    access_key TEXT NOT NULL,
    at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_auth_failures ON auth_failures (access_key, at);
CREATE TABLE IF NOT EXISTS approvals (
    id             TEXT PRIMARY KEY,
    action         TEXT NOT NULL,
    payload        TEXT NOT NULL,
    summary        TEXT NOT NULL,
    requested_user TEXT NOT NULL,
    requested_by   TEXT NOT NULL,
    rule           TEXT NOT NULL,
    status         TEXT NOT NULL,
    created_at     INTEGER NOT NULL,
    expires_at     INTEGER NOT NULL,
    decided_by     TEXT,
    decided_at     INTEGER,
    result         TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_user ON approvals (requested_user, status);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    module     TEXT NOT NULL,
    type       TEXT NOT NULL,
    user       TEXT,
    title      TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_user ON events (user, id);
"""


class Database:
    """SQLite 연결 하나를 감싼 클래스. 모든 읽기/쓰기는 tx()를 거친다."""

    def __init__(self, path: Path):
        """DB 파일을 열고(없으면 만들고) 테이블을 준비한다.

        check_same_thread=False: 여러 스레드가 이 연결 하나를 같이 쓴다(동시 접근은 _lock으로 막는다).
        isolation_level=None: 트랜잭션 시작/끝을 tx()에서 직접 정한다.
        WAL: 쓰는 동안에도 읽기가 막히지 않는 저널 방식.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """트랜잭션 하나를 연다. with 블록이 무사히 끝나면 COMMIT, 예외가 나면 ROLLBACK.

            with db.tx() as c:
                c.execute("INSERT ...")

        BEGIN IMMEDIATE는 시작하자마자 쓰기 잠금을 잡아서, "읽고 → 확인하고 → 쓰기" 사이에
        다른 요청이 끼어들지 못하게 한다(Nonce 중복 확인, 승인 한 번만 처리에 필요).
        """
        with _lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        """DB 연결을 닫는다. 서버 종료 때, CLI가 잠깐 열었다 닫을 때 쓴다."""
        self._conn.close()
