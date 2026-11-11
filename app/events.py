"""모듈이 허브에 알려 오는 이벤트 (카카오 알림이 왔다, 요약이 끝났다 ...).

허브는 이벤트를 짧게 보관만 한다. 클라이언트가 GET /v1/events 로 가져간다.
제목만 남기고 본문 같은 민감한 내용은 받지 않는다.
"""
import time
from dataclasses import asdict, dataclass

from .db import Database

KEEP_SECONDS = 7 * 24 * 3600


@dataclass
class Event:
    """이벤트 하나. user가 None이면 가족 모두에게 보인다."""

    id: int
    module: str
    type: str
    user: str | None
    title: str
    created_at: int

    def public(self) -> dict:
        """API 응답용 사전(dict)."""
        return asdict(self)


class EventStore:
    """이벤트 보관함. 넣을 때마다 7일 지난 것을 지운다."""

    def __init__(self, db: Database):
        """DB를 받는다. 테이블은 db.py의 SCHEMA가 만든다."""
        self.db = db

    def add(self, module: str, type_: str, user: str | None, title: str) -> int:
        """이벤트를 저장하고 번호를 돌려준다."""
        now = int(time.time())
        with self.db.tx() as c:
            c.execute("DELETE FROM events WHERE created_at < ?", (now - KEEP_SECONDS,))
            cur = c.execute(
                "INSERT INTO events (module, type, user, title, created_at) VALUES (?, ?, ?, ?, ?)",
                (module, type_, user, title, now),
            )
            return cur.lastrowid

    def list_for(self, user_id: str, after: int = 0, limit: int = 50) -> list[Event]:
        """이 사람에게 보이는 이벤트(본인 것 + 모두에게 보이는 것)를 after 번호 다음부터 돌려준다.

        클라이언트는 마지막으로 받은 id를 기억했다가 after로 넘기면 새 이벤트만 받는다.
        """
        with self.db.tx() as c:
            rows = c.execute(
                "SELECT * FROM events WHERE (user = ? OR user IS NULL) AND id > ? ORDER BY id LIMIT ?",
                (user_id, after, limit),
            ).fetchall()
        return [Event(**dict(r)) for r in rows]
