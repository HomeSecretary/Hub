"""승인 대기열.

모듈이 manifest에 "approval": true 로 적은 액션은 바로 실행하지 않고 여기 올린다.
대기열은 모듈 내용을 모른다. action("mail.send")과 payload(모듈, 액션, 입력값)를 보관했다가
승인되면 그대로 모듈에 넘길 뿐이다. 누가 승인할 수 있는지는 요청할 때 정해진다.

  same_user : 요청한 사람의 "다른" 기기만 승인할 수 있다 (관리자, 어른 가족)
  admin     : 관리자의 기기만 승인할 수 있다 (아이의 요청)

어느 경우든 요청한 기기 자신은 승인할 수 없고, 다른 가족의 요청은 보이지도 않는다.
"""
import json
import secrets
import time
from dataclasses import dataclass

from .auth.scopes import ADMIN, CHILD
from .db import Database

PENDING, APPROVED, REJECTED, EXPIRED, FAILED = "pending", "approved", "rejected", "expired", "failed"
RULE_SAME_USER, RULE_ADMIN = "same_user", "admin"


class ApprovalError(Exception):
    """승인 처리 중 규칙 위반. code는 HTTP 상태로 바꾸는 데 쓰고, message는 사용자에게 보여 준다."""

    def __init__(self, code: str, message: str):
        """code(예: not_found, same_device)와 사용자에게 보여 줄 message를 함께 담는다."""
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Approval:
    """승인 대기열의 요청 하나 (DB approvals 테이블의 한 줄)."""

    id: str
    action: str
    payload: dict
    summary: str
    requested_user: str
    requested_by: str
    rule: str
    status: str
    created_at: int
    expires_at: int
    decided_by: str | None
    decided_at: int | None
    result: str | None

    def public(self) -> dict:
        """API 응답용 사전(dict). payload(메일 본문 등)는 빼고 요약만 담는다."""
        # 대기열 조회에는 본문 전체 대신 요약만 내보낸다. 워치나 안경 화면에는 이걸로 충분하다.
        return {
            "id": self.id,
            "action": self.action,
            "summary": self.summary,
            "requested_user": self.requested_user,
            "requested_by": self.requested_by,
            "rule": self.rule,
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "result": self.result,
        }


def _row(r) -> Approval:
    """DB에서 꺼낸 한 줄을 Approval 객체로 바꾼다. payload는 JSON 문자열이라 다시 dict로 풀어 준다."""
    return Approval(
        id=r["id"], action=r["action"], payload=json.loads(r["payload"]), summary=r["summary"],
        requested_user=r["requested_user"], requested_by=r["requested_by"], rule=r["rule"], status=r["status"],
        created_at=r["created_at"], expires_at=r["expires_at"], decided_by=r["decided_by"],
        decided_at=r["decided_at"], result=r["result"],
    )


def rule_for(role: str) -> str:
    """요청한 사람의 역할로 승인 규칙을 정한다. 아이는 관리자 승인(admin), 나머지는 본인 다른 기기 승인(same_user)."""
    return RULE_ADMIN if role == CHILD else RULE_SAME_USER


def can_decide(approval: Approval, user_id: str, role: str) -> bool:
    """이 사람이 이 요청을 승인/거절할 수 있는지.

    admin 규칙    : 관리자이면서 요청한 본인이 아닐 때
    same_user 규칙: 요청한 본인일 때 (같은 기기인지는 claim()에서 따로 막는다)
    """
    if approval.rule == RULE_ADMIN:
        return role == ADMIN and user_id != approval.requested_user
    return user_id == approval.requested_user


class ApprovalQueue:
    """승인 대기열. 요청을 올리고(create), 찾고(get, list_for), 결정하고(claim), 결과를 남긴다(set_result)."""

    def __init__(self, db: Database, ttl_seconds: int):
        """DB와 요청 유효 시간(초)을 받는다. 이 시간이 지나도록 아무도 결정하지 않으면 만료된다."""
        self.db = db
        self.ttl = ttl_seconds

    def _expire(self, c, now: int) -> None:
        """유효 시간이 지난 pending 요청을 expired로 바꾼다.

        따로 도는 타이머 없이, 대기열을 읽거나 쓸 때마다 먼저 이걸 불러 정리한다.
        """
        c.execute("UPDATE approvals SET status = ? WHERE status = ? AND expires_at < ?", (EXPIRED, PENDING, now))

    def create(self, action: str, payload: dict, summary: str, user_id: str, role: str, device_key: str) -> Approval:
        """요청을 pending 상태로 대기열에 올리고 저장된 Approval을 돌려준다.

        ID는 추측하기 어려운 무작위 문자열. 승인 규칙(rule_for)은 이때 정해져 나중에 바뀌지 않는다.
        """
        now = int(time.time())
        approval_id = secrets.token_urlsafe(9)
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO approvals (id, action, payload, summary, requested_user, requested_by, rule, status,"
                " created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (approval_id, action, json.dumps(payload, ensure_ascii=False), summary, user_id, device_key,
                 rule_for(role), PENDING, now, now + self.ttl),
            )
        return self.get(approval_id)

    def get(self, approval_id: str) -> Approval | None:
        """ID로 요청 하나를 찾는다. 없으면 None. 읽기 전에 만료 처리를 먼저 한다."""
        with self.db.tx() as c:
            self._expire(c, int(time.time()))
            r = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        return _row(r) if r else None

    def list_for(self, user_id: str, role: str, status: str | None = PENDING, limit: int = 20) -> list[Approval]:
        """이 사람이 결정할 수 있는 요청만 돌려준다. 다른 가족의 요청은 보이지 않는다."""
        where = ["((rule = ? AND requested_user = ?)"]
        args: list = [RULE_SAME_USER, user_id]
        if role == ADMIN:
            where.append(" OR (rule = ? AND requested_user != ?)")
            args += [RULE_ADMIN, user_id]
        where.append(")")
        sql = "SELECT * FROM approvals WHERE " + "".join(where)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.db.tx() as c:
            self._expire(c, int(time.time()))
            rows = c.execute(sql, args).fetchall()
        return [_row(r) for r in rows]

    def claim(self, approval_id: str, user_id: str, role: str, device_key: str, decision: str) -> Approval:
        """pending → approved/rejected 로 한 번만 바꾼다. 동시에 두 번 승인돼도 실행은 한 번."""
        now = int(time.time())
        with self.db.tx() as c:
            self._expire(c, now)
            r = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
            # 남의 요청은 "없는 요청"과 똑같이 대답한다. 있다는 사실도 알려 주지 않는다.
            if r is None or not can_decide(_row(r), user_id, role):
                raise ApprovalError("not_found", "요청을 찾을 수 없습니다.")
            if r["requested_by"] == device_key:
                raise ApprovalError("same_device", "요청한 기기에서는 승인할 수 없습니다. 다른 기기에서 승인해 주세요.")
            if r["status"] != PENDING:
                raise ApprovalError("not_pending", "이미 처리됐거나 만료된 요청입니다.")
            c.execute(
                "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ? WHERE id = ?",
                (decision, device_key, now, approval_id),
            )
        return self.get(approval_id)

    def has_history(self, user_id: str) -> bool:
        """이 사람이 요청한 기록이 하나라도 있는지 (상태 무관)."""
        with self.db.tx() as c:
            return c.execute("SELECT 1 FROM approvals WHERE requested_user = ? LIMIT 1", (user_id,)).fetchone() is not None

    def set_result(self, approval_id: str, status: str, result: str) -> Approval:
        """실행이 끝난 요청에 최종 상태(approved/failed)와 결과 문구를 기록한다."""
        with self.db.tx() as c:
            c.execute("UPDATE approvals SET status = ?, result = ? WHERE id = ?", (status, result, approval_id))
        return self.get(approval_id)
