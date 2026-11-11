"""모든 /v1 요청 앞에서 도는 인증 의존성.

확인 순서:
  1. Cloudflare Access를 거쳐 왔는지 (cloudflared가 붙이는 JWT 헤더)
  2. 헤더 형식, 본문 크기
  3. 키 잠금 여부 (실패가 몰린 키는 잠시 차단)
  4. 기기 존재 여부와 폐기 여부, 기기 주인(사용자)이 활성 상태인지
  5. 타임스탬프 허용 범위
  6. 서명 일치 (상수 시간 비교)
  7. Nonce 재사용 여부
  8. scope (기기 scope AND 사용자 역할이 허용하는 scope)

실패 원인은 감사 로그에만 남기고, 응답은 항상 같은 401 한 가지로 돌려준다.
어디서 막혔는지 알려 주면 공격자에게 힌트가 되기 때문이다.
"""
import hmac
import re
import time
from dataclasses import dataclass

from fastapi import HTTPException, Request, status

from .registry import Device, User
from .scopes import allows, effective_scopes
from .signing import (
    HEADER_KEY,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_TIMESTAMP,
    canonical_string,
    sign,
)

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_TS_RE = re.compile(r"^\d{9,11}$")

UNAUTHORIZED = HTTPException(status.HTTP_401_UNAUTHORIZED, detail="인증에 실패했습니다.")
FORBIDDEN = HTTPException(status.HTTP_403_FORBIDDEN, detail="권한이 없습니다.")


@dataclass
class Caller:
    """인증을 통과한 호출자. 라우터 함수는 이걸 받아 누가(user), 어떤 기기로(device), 무엇을 할 수 있는지(scopes) 안다."""

    user: User
    device: Device
    scopes: set[str]
    ip: str | None

    def can(self, scope: str) -> bool:
        """이 호출자가 scope 하나를 쓸 수 있는지. 와일드카드(mail:*, modules:*)도 따진다."""
        return allows(self.scopes, scope)

    def forbid(self, request: Request, scope: str) -> HTTPException:
        """권한 없음을 감사 로그에 남기고, 돌려줄 403 예외를 만든다. 부르는 쪽에서 raise 한다."""
        request.app.state.audit.write(
            "auth.forbidden", user=self.user.id, device=self.device.access_key, ip=self.ip,
            scope=scope, path=request.url.path,
        )
        return FORBIDDEN


def _client_ip(request: Request) -> str | None:
    """요청한 쪽의 실제 IP를 찾는다. 감사 로그에 남기는 용도."""
    # Cloudflare를 거친 요청은 이 헤더에 실제 클라이언트 IP가 있다.
    return request.headers.get("Cf-Connecting-Ip") or (request.client.host if request.client else None)


async def authenticate(request: Request) -> Caller:
    """요청 헤더의 서명을 검사해 호출자(Caller)를 돌려준다. 하나라도 틀리면 401.

    검사 순서는 이 파일 맨 위 설명 참고. 비용이 싼 검사(헤더 형식)를 먼저,
    비싼 검사(DB 조회, 서명 계산)를 나중에 한다.
    """
    state = request.app.state
    settings = state.settings
    audit = state.audit
    ip = _client_ip(request)

    def fail(reason: str, key: str | None = None) -> HTTPException:
        """인증 실패를 감사 로그와 실패 기록 테이블에 남기고, 돌려줄 401 예외를 만든다.

        부르는 쪽에서 raise fail("이유") 로 쓴다. 이유는 로그에만 남고 응답에는 나가지 않는다.
        키 형식이 정상일 때만 실패 기록을 쌓는다(이상한 값으로 테이블을 채우지 못하게).
        """
        audit.write("auth.fail", device=key, ip=ip, reason=reason, path=request.url.path)
        if key and _KEY_RE.match(key):
            with state.db.tx() as c:
                c.execute("INSERT INTO auth_failures (access_key, at) VALUES (?, ?)", (key, int(time.time())))
        return UNAUTHORIZED

    if settings.require_cf_access and not request.headers.get("Cf-Access-Jwt-Assertion"):
        raise fail("no_cf_access")

    key = request.headers.get(HEADER_KEY, "")
    ts = request.headers.get(HEADER_TIMESTAMP, "")
    nonce = request.headers.get(HEADER_NONCE, "")
    signature = request.headers.get(HEADER_SIGNATURE, "")
    if not (_KEY_RE.match(key) and _TS_RE.match(ts) and _NONCE_RE.match(nonce) and signature):
        raise fail("bad_headers", key or None)

    body = await request.body()
    if len(body) > settings.max_body_bytes:
        raise fail("body_too_large", key)

    now = int(time.time())
    with state.db.tx() as c:
        c.execute("DELETE FROM auth_failures WHERE at < ?", (now - settings.lockout_window_seconds,))
        failures = c.execute(
            "SELECT COUNT(*) FROM auth_failures WHERE access_key = ? AND at > ?",
            (key, now - settings.lockout_window_seconds),
        ).fetchone()[0]
    if failures >= settings.lockout_threshold:
        audit.write("auth.locked", device=key, ip=ip, path=request.url.path)
        raise UNAUTHORIZED

    device = state.registry.device(key)
    if device is None:
        raise fail("unknown_key", key)
    if device.revoked:
        raise fail("revoked_key", key)
    user = state.registry.user(device.user)
    if user is None or user.disabled:
        raise fail("user_disabled", key)

    if abs(now - int(ts)) > settings.clock_skew_seconds:
        raise fail("timestamp_skew", key)

    canonical = canonical_string(request.method, request.url.path, request.url.query, ts, nonce, body)
    expected = sign(device.secret, canonical)
    if not hmac.compare_digest(expected, signature):
        raise fail("bad_signature", key)

    # Nonce는 타임스탬프 허용 범위의 두 배 동안 기억한다. 그 뒤에는 타임스탬프 검사에서 걸린다.
    with state.db.tx() as c:
        c.execute("DELETE FROM nonces WHERE expires_at < ?", (now,))
        exists = c.execute(
            "SELECT 1 FROM nonces WHERE access_key = ? AND nonce = ?", (key, nonce)
        ).fetchone()
        if not exists:
            c.execute(
                "INSERT INTO nonces (access_key, nonce, expires_at) VALUES (?, ?, ?)",
                (key, nonce, now + settings.clock_skew_seconds * 2),
            )
    if exists:
        raise fail("nonce_replay", key)

    return Caller(user=user, device=device, scopes=effective_scopes(device.scopes, user.role), ip=ip)


def require(scope: str):
    """scope 하나를 받아 "이 권한이 있어야 통과"하는 의존성 함수를 만들어 돌려준다.

        @router.get("/me")
        async def me(caller: Caller = Depends(require(SYSTEM_READ))):

    인증(401)을 먼저 하고, scope가 없으면 403. scope마다 검사 함수를 찍어 내는 함수다.
    """
    async def dependency(request: Request) -> Caller:
        """FastAPI가 요청마다 실제로 부르는 함수. 인증 후 scope를 확인한다."""
        caller = await authenticate(request)
        if not caller.can(scope):
            raise caller.forbid(request, scope)
        return caller

    return dependency
