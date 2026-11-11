"""허브 설정. SECRETARY_ 로 시작하는 환경 변수(또는 .env)에서 읽는다."""
import re
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

MODULE_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,23}")
# 허브가 직접 쓰는 scope 앞부분이라 모듈 이름으로 쓸 수 없다
RESERVED_MODULE_NAMES = {"system", "approvals", "modules", "events"}


def _pairs(raw: str) -> dict[str, str]:
    """ "mail=http://a,tapo=http://b" → {"mail": "http://a", "tapo": "http://b"}. 빈 칸은 무시한다."""
    out = {}
    for item in raw.split(","):
        if not item.strip():
            continue
        name, sep, value = item.partition("=")
        if not sep:
            raise ValueError(f"'이름=값' 형식이 아닙니다: {item.strip()}")
        out[name.strip()] = value.strip()
    return out


class Settings(BaseSettings):
    """허브 설정. 예: SECRETARY_MAX_USERS=3 → max_users = 3. 환경 변수가 없으면 아래 기본값을 쓴다."""

    model_config = SettingsConfigDict(env_prefix="SECRETARY_", env_file=".env", extra="ignore")

    data_dir: Path = Path("/data")

    # 한 집에서 쓰는 서비스라 활성 사용자 수에 상한을 둔다
    max_users: int = 4

    # 인증
    clock_skew_seconds: int = 60
    max_body_bytes: int = 64 * 1024
    require_cf_access: bool = True
    lockout_threshold: int = 10        # 이 횟수만큼 실패하면
    lockout_window_seconds: int = 300  # 이 시간 동안 해당 키를 잠근다

    # 승인
    approval_ttl_seconds: int = 300

    # 모듈 (docs/module-contract.md)
    modules: str = ""         # mail=http://secretary-mail:8080,tapo=http://secretary-tapo:8080
    module_tokens: str = ""   # mail=<토큰>,tapo=<토큰>
    module_timeout_seconds: float = 10.0

    @property
    def db_path(self) -> Path:
        """SQLite 파일 위치 (Nonce, 인증 실패, 승인 대기열, 이벤트)."""
        return self.data_dir / "secretary.db"

    @property
    def registry_path(self) -> Path:
        """사용자·기기 저장 파일 위치. 기기 Secret이 들어 있어 권한 600으로 만든다."""
        return self.data_dir / "registry.json"

    @property
    def audit_path(self) -> Path:
        """감사 로그 파일 위치."""
        return self.data_dir / "audit.log"

    @property
    def module_endpoints(self) -> dict[str, tuple[str, str]]:
        """모듈 이름 → (주소, 토큰). 이름 규칙이 틀리거나 토큰이 빠졌으면 서버가 아예 뜨지 않게 오류를 낸다."""
        urls, tokens = _pairs(self.modules), _pairs(self.module_tokens)
        out = {}
        for name, url in urls.items():
            if not MODULE_NAME_RE.fullmatch(name) or name in RESERVED_MODULE_NAMES:
                raise ValueError(f"모듈 이름으로 쓸 수 없습니다: {name}")
            token = tokens.get(name, "")
            if len(token) < 32:
                raise ValueError(f"모듈 {name}의 토큰이 없거나 너무 짧습니다 (32자 이상)")
            out[name] = (url.rstrip("/"), token)
        return out


@lru_cache
def get_settings() -> Settings:
    """설정 객체를 돌려준다. @lru_cache 덕분에 처음 한 번만 만들고 그 뒤로는 같은 객체를 재사용한다."""
    return Settings()
