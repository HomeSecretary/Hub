"""이벤트: 모듈 → 허브 (POST /internal/events), 허브 → 클라이언트 (GET /v1/events)."""
import hmac

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from ..auth.guard import Caller, require
from ..auth.scopes import SYSTEM_READ

router = APIRouter(prefix="/v1", tags=["events"])
internal = APIRouter(prefix="/internal", include_in_schema=False)

NOT_FOUND = HTTPException(status.HTTP_404_NOT_FOUND, detail="Not Found")


class EventIn(BaseModel):
    """모듈이 보내는 이벤트. 제목만 받는다(200자까지)."""

    type: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,39}$")
    user: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9-]{1,23}$")
    title: str = Field(min_length=1, max_length=200)


def module_from_token(request: Request) -> str:
    """내부 토큰으로 어느 모듈이 보낸 요청인지 알아낸다.

    Cloudflare를 거쳐 온 요청(Cf- 헤더가 붙어 있음)은 바깥에서 온 것이라 없는 경로처럼 404.
    토큰이 어느 모듈과도 맞지 않으면 401. 토큰 비교는 상수 시간 비교(compare_digest).
    """
    if request.headers.get("Cf-Connecting-Ip") or request.headers.get("Cf-Access-Jwt-Assertion"):
        raise NOT_FOUND
    auth = request.headers.get("Authorization", "")
    token = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
    for name, (_, expected) in request.app.state.catalog.endpoints.items():
        if token and hmac.compare_digest(expected, token):
            return name
    request.app.state.audit.write("internal.auth_fail", ip=request.client.host if request.client else None)
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail="인증에 실패했습니다.")


@internal.post("/events", status_code=status.HTTP_201_CREATED)
async def receive_event(event: EventIn, request: Request, module: str = Depends(module_from_token)):
    """모듈이 보낸 이벤트를 저장한다. 감사 로그에는 제목을 남기지 않는다(개인 메시지일 수 있다)."""
    state = request.app.state
    if event.user is not None and state.registry.user(event.user) is None:
        raise HTTPException(422, detail="없는 사용자입니다.")
    event_id = state.events.add(module, event.type, event.user, event.title)
    state.audit.write("event.receive", user=event.user, module=module, type=event.type, event_id=event_id)
    return {"id": event_id}


@router.get("/events")
async def list_events(request: Request, after: int = Query(0, ge=0),
                      caller: Caller = Depends(require(SYSTEM_READ))):
    """내게 온 이벤트와 가족 모두에게 온 이벤트. after에 마지막으로 받은 id를 넣으면 새것만 온다."""
    items = request.app.state.events.list_for(caller.user.id, after)
    return {"items": [e.public() for e in items]}
