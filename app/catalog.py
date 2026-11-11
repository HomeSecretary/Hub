"""모듈 카탈로그. 허브가 아는 모듈 목록과, 모듈을 부르는 HTTP 클라이언트.

허브는 모듈의 코드를 import 하지 않는다. 설정에 적힌 주소로 manifest(소개서)를 받아 오고,
명령이 오면 그 주소로 HTTP 요청을 보낼 뿐이다. 약속(계약)은 docs/module-contract.md 참고.
"""
import logging
import re
import time

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger("secretary.catalog")

MANIFEST_TTL_SECONDS = 60
# 액션 이름은 URL 경로와 scope에 들어가므로 영문 소문자, 숫자, _, - 만
ACTION_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


class ModuleError(Exception):
    """모듈을 부르다 실패했을 때. status는 클라이언트에게 돌려줄 HTTP 상태 코드."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class ActionSpec(BaseModel):
    """manifest 안의 액션 하나. 필요한 scope는 따로 적지 않고 "<모듈>:<액션>"으로 정해진다."""

    title: str = Field(max_length=60)
    approval: bool = False
    roles: list[str] | None = None
    params: dict | None = None


class Manifest(BaseModel):
    """모듈 소개서. GET /manifest 응답을 이 모양으로 검사한다. 모양이 틀린 모듈은 쓰지 않는다."""

    name: str
    title: str = Field(max_length=60)
    actions: dict[str, ActionSpec]

    @field_validator("actions")
    @classmethod
    def _action_names(cls, actions: dict) -> dict:
        """액션 이름 규칙(ACTION_RE)에 맞지 않는 액션이 하나라도 있으면 manifest 전체를 거부한다."""
        bad = [a for a in actions if not ACTION_RE.fullmatch(a)]
        if bad:
            raise ValueError(f"bad action names: {bad}")
        return actions


class ModuleCatalog:
    """설정에 등록된 모듈들의 manifest를 기억하고, 모듈에 요청을 보낸다."""

    def __init__(self, endpoints: dict[str, tuple[str, str]], timeout: float = 10.0, transport=None):
        """endpoints: 모듈 이름 → (주소, 토큰). transport는 테스트에서 가짜 모듈을 붙일 때만 넣는다."""
        self.endpoints = endpoints
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._cache: dict[str, tuple[float, Manifest]] = {}

    async def close(self) -> None:
        """HTTP 연결을 닫는다. 서버가 꺼질 때 부른다."""
        await self._http.aclose()

    def _headers(self, name: str) -> dict:
        """그 모듈의 내부 토큰을 붙인 헤더. 모듈은 이 토큰이 맞을 때만 요청을 받는다."""
        return {"Authorization": f"Bearer {self.endpoints[name][1]}"}

    async def manifest(self, name: str) -> Manifest:
        """모듈의 manifest를 돌려준다. 1분 동안은 기억해 둔 것을 쓰고, 지나면 다시 받아 온다.

        모듈이 꺼져 있으면 503. 다시 받아 오다 실패해도 전에 받아 둔 manifest가 있으면 그걸 쓴다.
        """
        if name not in self.endpoints:
            raise ModuleError(404, "없는 모듈입니다.")
        cached = self._cache.get(name)
        if cached and time.monotonic() - cached[0] < MANIFEST_TTL_SECONDS:
            return cached[1]
        url = self.endpoints[name][0]
        try:
            res = await self._http.get(f"{url}/manifest", headers=self._headers(name))
            res.raise_for_status()
            manifest = Manifest.model_validate(res.json())
            if manifest.name != name:
                raise ValueError(f"manifest name mismatch: {manifest.name}")
        except (httpx.HTTPError, ValidationError, ValueError) as e:
            logger.warning("module %s manifest failed: %s", name, e)
            if cached:
                return cached[1]
            raise ModuleError(503, "모듈에 연결할 수 없습니다.") from e
        self._cache[name] = (time.monotonic(), manifest)
        return manifest

    async def action(self, name: str, action: str) -> ActionSpec:
        """모듈의 액션 하나를 찾는다. 없으면 404."""
        spec = (await self.manifest(name)).actions.get(action)
        if spec is None:
            raise ModuleError(404, "없는 액션입니다.")
        return spec

    async def _post(self, name: str, path: str, params: dict, caller: dict) -> dict:
        """모듈에 POST를 보내고 JSON 응답을 돌려준다.

        모듈이 422를 주면 그 메시지를 그대로 사용자에게 보여 준다(입력이 틀린 것이므로).
        그 밖의 실패(연결 안 됨, 500, 401)는 자세한 내용을 서버 로그에만 남기고 502로 바꾼다.
        """
        url = self.endpoints[name][0]
        try:
            res = await self._http.post(f"{url}{path}", json={"params": params, "caller": caller},
                                        headers=self._headers(name))
        except httpx.HTTPError as e:
            logger.warning("module %s %s failed: %s", name, path, e)
            raise ModuleError(502, "모듈에 연결할 수 없습니다.") from e
        if res.status_code == 422:
            try:
                message = str(res.json().get("message", ""))[:200]
            except ValueError:
                message = ""
            raise ModuleError(422, message or "요청 내용이 올바르지 않습니다.")
        if res.status_code != 200:
            logger.warning("module %s %s returned %s", name, path, res.status_code)
            raise ModuleError(502, "모듈 실행에 실패했습니다.")
        return res.json()

    async def validate(self, name: str, action: str, params: dict, caller: dict) -> str:
        """실행 전에 입력을 검사받고, 승인 화면에 보여 줄 한 줄 요약을 받아 온다."""
        data = await self._post(name, f"/actions/{action}/validate", params, caller)
        return str(data.get("summary", f"{name}.{action}"))[:200]

    async def execute(self, name: str, action: str, params: dict, caller: dict) -> str:
        """모듈에 실제 실행을 맡기고 결과 문구를 돌려준다."""
        data = await self._post(name, f"/actions/{action}", params, caller)
        return str(data.get("result", "완료"))[:200]


def caller_context(user) -> dict:
    """모듈에 넘길 "누가 시켰는지" 정보. 기기 키나 Secret 같은 인증 정보는 절대 넘기지 않는다."""
    return {"user_id": user.id, "name": user.name, "role": user.role, "email": user.email}
