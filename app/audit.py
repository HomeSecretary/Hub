"""감사 로그. 한 줄에 JSON 하나(JSONL).

비밀값과 메일 본문은 남기지 않는다. 누가(사람, 기기), 언제, 무엇을, 결과가 어땠는지만 남긴다.
"""
import json
import logging
import threading
import time
from pathlib import Path

logger = logging.getLogger("secretary.audit")
_lock = threading.Lock()


class AuditLog:
    """감사 로그 기록기. 파일 끝에 한 줄씩 덧붙이기만 하고, 고치거나 지우지 않는다."""

    def __init__(self, path: Path):
        """로그 파일 경로를 기억하고, 폴더가 없으면 만든다."""
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, *, user: str | None = None, device: str | None = None, ip: str | None = None,
              **fields) -> None:
        """사건 하나를 JSON 한 줄로 기록한다. 서버 로그(docker logs)에도 같은 줄을 남긴다.

        event : 무슨 일인지. 예) "auth.fail", "approval.approve"
        user, device, ip : 누가, 어떤 기기로, 어디서
        **fields : 그 밖의 정보. 예) reason="bad_signature". 비밀값과 메일 본문은 넣지 않는다.
        """
        record = {"ts": int(time.time()), "event": event, "user": user, "device": device, "ip": ip, **fields}
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        logger.info(line)
