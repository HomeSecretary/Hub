"""사용자와 기기 저장소 (data/registry.json).

사람(user)이 있고, 기기(device)는 사람에게 속한다. 한 집에서 쓰는 서비스라
활성 사용자 수에 상한(기본 4명)을 둔다.

HMAC은 서버도 같은 비밀값으로 서명을 다시 계산해야 하므로 기기 Secret을 원문으로
보관한다. 그래서 파일을 처음부터 권한 600으로 만들고, 이 파일은 데이터 볼륨 밖으로
내보내지 않는다.
"""
import json
import os
import re
import secrets
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .scopes import ADMIN, ROLES, is_valid_scope

# 사용자 ID와 기기 이름 규칙. fullmatch()로 쓰기 때문에 문자열 "전체"가 이 모양이어야 한다.
# (Access Key 앞부분이 되고 HTTP 헤더로 오가므로 영문 소문자, 숫자, 하이픈만 허용)
#   [a-z0-9]         첫 글자: 영문 소문자 또는 숫자 (하이픈으로 시작할 수 없다)
#   [a-z0-9-]{1,23}  나머지 1~23자: 영문 소문자, 숫자, 하이픈
#   → 전체 2~24자
#   통과: dad, kid1, phone-flutter
#   거부: -dad (하이픈 시작), Dad (대문자), 내폰 (한글), a (1자), 25자 이상
_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,23}")

# 이메일 형식 확인. 실제로 존재하는 주소인지가 아니라 "이메일처럼 생겼는지"만 가볍게 본다.
#   [^@\s]+  '@'도 공백도 아닌 글자 1개 이상 → 아이디 부분 (dad)
#   @        '@' 기호 딱 하나
#   [^@\s]+  '@'도 공백도 아닌 글자 1개 이상 → 도메인 이름 (example)
#   \.       점(.) 하나. 정규식에서 '.'은 "아무 글자"라는 뜻이라 \ 를 붙여 진짜 점으로 쓴다
#   [^@\s]+  점 뒤의 나머지 → (com, co.kr)
#   ([^...]는 "괄호 안의 글자가 아닌 것", \s는 공백 문자, +는 "1개 이상")
#   통과: dad@example.com, mom@mail.co.kr
#   거부: dad@example (점 없음), dad@@example.com (@ 두 개), "a b@c.d" (공백)
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


class RegistryError(ValueError):
    """사용자·기기 등록 규칙을 어겼을 때 내는 오류. 메시지는 그대로 CLI 화면에 보여 준다."""


@dataclass
class User:
    """가족 한 사람. role은 admin/member/child 중 하나, email은 메일 Reply-To에 쓴다."""

    id: str
    name: str
    role: str
    created_at: int
    email: str | None = None
    disabled: bool = False

    def public(self) -> dict:
        """API 응답에 내보낼 정보만 고른다 (이메일은 빼고 ID, 이름, 역할만)."""
        return {"id": self.id, "name": self.name, "role": self.role}


@dataclass
class Device:
    """사람에게 속한 기기 하나 (폰, 워치, 안경...). access_key가 기기 ID이고 secret이 서명용 비밀값.

    폐기해도 지우지 않고 revoked=True로 남긴다. 감사 로그가 가리키는 키를 나중에도 알아볼 수 있게.
    """

    access_key: str
    user: str
    name: str
    secret: str
    scopes: list[str]
    created_at: int
    revoked: bool = False
    revoked_at: int | None = None


class Registry:
    """사용자와 기기를 registry.json 파일 하나에 저장하고 꺼내 쓰는 저장소."""

    def __init__(self, path: Path, max_users: int = 4):
        """파일 경로와 최대 사용자 수를 받고, 파일이 있으면 바로 읽어 둔다."""
        self.path = path
        self.max_users = max_users
        self._lock = threading.Lock()
        self._users: dict[str, User] = {}
        self._devices: dict[str, Device] = {}
        self._mtime: float | None = None
        self._load()

    def _load(self) -> None:
        """registry.json을 읽어 메모리에 올린다. 파일 수정 시각이 바뀌었을 때만 다시 읽는다.

        CLI(docker exec)는 서버와 다른 프로세스라서, CLI로 기기를 추가하면 파일만 바뀐다.
        그래서 매번 수정 시각을 보고, 바뀌었으면 서버를 재시작하지 않아도 새 내용을 읽는다.
        """
        if not self.path.exists():
            self._users, self._devices, self._mtime = {}, {}, None
            return
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self._users = {k: User(**v) for k, v in raw.get("users", {}).items()}
        self._devices = {k: Device(**v) for k, v in raw.get("devices", {}).items()}
        self._mtime = mtime

    def _save(self) -> None:
        """메모리 내용을 registry.json에 쓴다.

        1. 임시 파일(.tmp)을 처음부터 권한 600(나만 읽고 쓰기)으로 만들어 쓰고
        2. os.replace로 원래 파일과 한 번에 바꿔치기한다.
        쓰는 도중 서버가 죽어도 원래 파일이 반쯤 쓰인 채로 남지 않는다.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = json.dumps(
            {
                "users": {k: asdict(v) for k, v in self._users.items()},
                "devices": {k: asdict(v) for k, v in self._devices.items()},
            },
            ensure_ascii=False,
            indent=2,
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime

    # ── 사용자 ─────────────────────────────────────────────

    def user(self, user_id: str) -> User | None:
        """ID로 사용자 한 명을 찾는다. 없으면 None."""
        with self._lock:
            self._load()
            return self._users.get(user_id)

    def users(self) -> list[User]:
        """모든 사용자 목록 (비활성 사용자 포함)."""
        with self._lock:
            self._load()
            return list(self._users.values())

    def add_user(self, user_id: str, name: str, role: str, email: str | None = None) -> User:
        """사용자를 추가한다. 규칙에 어긋나면 RegistryError.

        - ID는 영문 소문자·숫자·하이픈 2~24자, 역할은 admin/member/child 중 하나
        - 활성 사용자는 max_users명까지
        - 첫 사용자(활성 관리자가 아무도 없을 때)는 admin이어야 한다
        """
        if not _ID_RE.fullmatch(user_id):
            raise RegistryError("사용자 ID는 영문 소문자, 숫자, 하이픈으로 2~24자여야 합니다. 예: dad, mom, kid1")
        if role not in ROLES:
            raise RegistryError(f"역할은 {', '.join(ROLES)} 중 하나여야 합니다.")
        if email and not _EMAIL_RE.fullmatch(email):
            raise RegistryError("이메일 형식이 올바르지 않습니다.")
        with self._lock:
            self._load()
            if user_id in self._users:
                raise RegistryError("이미 있는 사용자 ID입니다.")
            active = [u for u in self._users.values() if not u.disabled]
            if len(active) >= self.max_users:
                raise RegistryError(f"사용자는 최대 {self.max_users}명까지 등록할 수 있습니다.")
            if not any(u.role == ADMIN for u in active) and role != ADMIN:
                raise RegistryError("첫 사용자는 관리자(admin)여야 합니다.")
            user = User(id=user_id, name=name, role=role, email=email, created_at=int(time.time()))
            self._users[user_id] = user
            self._save()
            return user

    def disable_user(self, user_id: str) -> int:
        """사용자를 비활성화하고 그 사람의 기기 키를 전부 폐기한다. 폐기한 키 개수를 돌려준다."""
        with self._lock:
            self._load()
            user = self._users.get(user_id)
            if not user or user.disabled:
                raise RegistryError("해당 사용자가 없거나 이미 비활성화됐습니다.")
            others = [u for u in self._users.values() if u.id != user_id and not u.disabled and u.role == ADMIN]
            if user.role == ADMIN and not others:
                raise RegistryError("마지막 관리자는 비활성화할 수 없습니다.")
            user.disabled = True
            count = self._revoke_where(lambda d: d.user == user_id)
            self._save()
            return count

    def delete_user(self, user_id: str, has_history: bool) -> User:
        """사용자를 완전히 지운다. 오타로 만든 사용자처럼 흔적이 없는 경우만 허용한다.

        기기(폐기된 것 포함)나 승인 기록이 있으면 지우지 않는다. 감사 로그와 승인 기록이
        이 ID를 가리키고 있어서, 지우면 나중에 누가 했는지 알 수 없게 되기 때문이다.
        그런 사용자는 disable_user()로 비활성화한다.
        """
        with self._lock:
            self._load()
            user = self._users.get(user_id)
            if not user:
                raise RegistryError("해당 사용자가 없습니다.")
            if any(d.user == user_id for d in self._devices.values()):
                raise RegistryError("기기가 등록된 적 있는 사용자는 지울 수 없습니다. 대신 user disable 을 쓰세요.")
            if has_history:
                raise RegistryError("승인 기록이 있는 사용자는 지울 수 없습니다. 대신 user disable 을 쓰세요.")
            if user.role == ADMIN and not user.disabled:
                others = [u for u in self._users.values() if u.id != user_id and not u.disabled and u.role == ADMIN]
                if not others and len(self._users) > 1:
                    raise RegistryError("다른 사용자가 있는 동안 마지막 관리자는 지울 수 없습니다.")
            del self._users[user_id]
            self._save()
            return user

    # ── 기기 ───────────────────────────────────────────────

    def device(self, access_key: str) -> Device | None:
        """Access Key로 기기 하나를 찾는다. 없으면 None. (폐기된 기기도 돌려주니 revoked를 따로 확인한다)"""
        with self._lock:
            self._load()
            return self._devices.get(access_key)

    def devices(self) -> list[Device]:
        """모든 기기 목록 (폐기된 기기 포함)."""
        with self._lock:
            self._load()
            return list(self._devices.values())

    def add_device(self, user_id: str, name: str, scopes: set[str]) -> Device:
        """사용자에게 기기 키를 새로 발급한다.

        Access Key = "사용자ID-기기이름-무작위8자" (예: dad-phone-1a2b3c4d)
        Secret     = 무작위 32바이트 (secrets.token_urlsafe: 추측할 수 없는 난수)
        scopes는 저장할 때 그대로 두고, 역할 상한은 요청할 때 effective_scopes()로 깎는다.
        """
        # 이름이 Access Key 앞부분이 되고 HTTP 헤더로 오가므로 영문 소문자, 숫자, 하이픈만 쓴다.
        if not _ID_RE.fullmatch(name):
            raise RegistryError("기기 이름은 영문 소문자, 숫자, 하이픈으로 2~24자여야 합니다. 예: phone, glasses")
        bad = {s for s in scopes if not is_valid_scope(s)}
        if bad:
            raise RegistryError(f"scope 모양이 올바르지 않습니다: {', '.join(sorted(bad))} (예: system:read, mail:send, mail:*, modules:*)")
        with self._lock:
            self._load()
            user = self._users.get(user_id)
            if not user or user.disabled:
                raise RegistryError("해당 사용자가 없거나 비활성화됐습니다.")
            access_key = f"{user_id}-{name}-{secrets.token_hex(4)}"
            device = Device(
                access_key=access_key,
                user=user_id,
                name=name,
                secret=secrets.token_urlsafe(32),
                scopes=sorted(scopes),
                created_at=int(time.time()),
            )
            self._devices[access_key] = device
            self._save()
            return device

    def _revoke_where(self, predicate) -> int:
        """조건(predicate)에 맞는 기기를 모두 폐기 표시하고, 폐기한 개수를 돌려준다.

        predicate는 기기를 받아 True/False를 돌려주는 함수. 예) lambda d: d.user == "kid1"
        저장(_save)은 부르는 쪽에서 한다.
        """
        now = int(time.time())
        count = 0
        for device in self._devices.values():
            if not device.revoked and predicate(device):
                device.revoked = True
                device.revoked_at = now
                count += 1
        return count

    def revoke(self, access_key: str) -> bool:
        """기기 키 하나를 폐기한다. 폐기했으면 True, 없거나 이미 폐기된 키면 False."""
        with self._lock:
            self._load()
            count = self._revoke_where(lambda d: d.access_key == access_key)
            if count:
                self._save()
            return bool(count)

    def revoke_all(self) -> int:
        """비상 정지: 모든 기기 키를 한 번에 무효화한다."""
        with self._lock:
            self._load()
            count = self._revoke_where(lambda d: True)
            if count:
                self._save()
            return count
