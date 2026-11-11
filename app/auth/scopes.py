"""권한 범위(scope), 역할(role), 기기 프리셋.

scope는 두 종류다.
  허브 scope   system:read, approvals:decide      허브가 직접 하는 일
  모듈 scope   <모듈>:<액션>  예) mail:send       모듈이 manifest에 적은 액션마다 자동으로 생긴다

모듈 scope는 와일드카드로 묶어서 줄 수 있다.
  mail:*      mail 모듈의 모든 액션
  modules:*   모든 모듈의 모든 액션 (나중에 추가될 모듈 포함)

실제로 쓸 수 있는 권한 = 기기에 준 scope AND 사용자 역할이 허용하는 scope.
그래서 아이 계정의 기기에 실수로 넓은 프리셋을 줘도 역할 상한을 넘지 못한다.
"""
import re

SYSTEM_READ = "system:read"            # 내 정보, 모듈 목록, 이벤트 조회
APPROVALS_DECIDE = "approvals:decide"  # 내가 결정할 수 있는 요청 조회, 승인, 거절
ALL_MODULES = "modules:*"              # 모든 모듈의 모든 액션

HUB_SCOPES = {SYSTEM_READ, APPROVALS_DECIDE}

# 모듈 scope 모양: 모듈 이름 ':' 액션 이름(또는 *)
_MODULE_SCOPE_RE = re.compile(r"([a-z0-9][a-z0-9-]{1,23}):([a-z0-9][a-z0-9_-]{0,31}|\*)")

# 역할
ADMIN = "admin"    # 집 주인. 모든 권한, 아이 요청 승인, 키 관리는 NAS CLI로
MEMBER = "member"  # 어른 가족. 자기 요청은 자기 기기로 승인
CHILD = "child"    # 아이. 승인 권한 없음. 승인이 필요한 요청은 관리자가 승인

ROLES = (ADMIN, MEMBER, CHILD)

# 역할 상한. 모듈별로 더 막고 싶으면 모듈 manifest의 roles 로 막는다.
ROLE_SCOPES: dict[str, set[str]] = {
    ADMIN: {SYSTEM_READ, APPROVALS_DECIDE, ALL_MODULES},
    MEMBER: {SYSTEM_READ, APPROVALS_DECIDE, ALL_MODULES},
    CHILD: {SYSTEM_READ, ALL_MODULES},
}

# 기기 종류별 추천 기본값 (CLI에서 --preset으로 사용)
PRESETS: dict[str, set[str]] = {
    "phone": {SYSTEM_READ, APPROVALS_DECIDE, ALL_MODULES},
    "watch": {SYSTEM_READ, APPROVALS_DECIDE},
    "glasses": {SYSTEM_READ, ALL_MODULES},
    "mcp": {SYSTEM_READ},
    "test": {SYSTEM_READ},
}


def module_of(scope: str) -> str | None:
    """모듈 scope면 모듈 이름을, 허브 scope면 None을 돌려준다. 예) "mail:send" → "mail", "modules:*" → "*" """
    if scope == ALL_MODULES:
        return "*"
    if scope in HUB_SCOPES:
        return None
    m = _MODULE_SCOPE_RE.fullmatch(scope)
    return m.group(1) if m else None


def is_valid_scope(scope: str) -> bool:
    """기기에 줄 수 있는 모양인지. 아직 없는 모듈의 scope도 미리 줄 수 있다."""
    return scope in HUB_SCOPES or module_of(scope) is not None


def allows(granted, required: str) -> bool:
    """granted(가진 scope들)가 required(필요한 scope 하나)를 허락하는지.

    정확히 같은 scope가 있거나, 모듈 scope라면 "<모듈>:*" 또는 "modules:*"가 있으면 허락한다.
    예) {"mail:*"} 는 "mail:send" 를 허락하고, "tapo:on" 은 허락하지 않는다.
    """
    granted = set(granted)
    if required in granted:
        return True
    module = module_of(required)
    if module is None or module == "*":
        return False
    return ALL_MODULES in granted or f"{module}:*" in granted


def effective_scopes(device_scopes, role: str) -> set[str]:
    """이 기기로 실제로 쓸 수 있는 권한 = 기기에 준 scope AND 역할이 허용하는 scope.

    기기 scope 하나하나를 역할 상한에 비춰 보고, 상한이 허락하는 것만 남긴다.
    예) 아이(child) 기기에 approvals:decide를 줘도 child 상한에 없으므로 빠진다.
    "modules:*" 자체는 상한에 그대로 있어야 남는다. 모르는 역할이면 빈 집합(아무 권한 없음).
    """
    cap = ROLE_SCOPES.get(role, set())
    return {s for s in device_scopes if s in cap or allows(cap, s)}
