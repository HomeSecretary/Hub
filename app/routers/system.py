from fastapi import APIRouter, Depends

from ..auth.guard import Caller, require
from ..auth.scopes import SYSTEM_READ

router = APIRouter(prefix="/v1", tags=["system"])


@router.get("/me")
async def me(caller: Caller = Depends(require(SYSTEM_READ))):
    """GET /v1/me : 지금 이 키가 누구의 어떤 기기이고, 무엇을 할 수 있는지 돌려준다.

    새 기기를 설정하고 처음 호출해 보는 용도. 200이 오면 서명과 Cloudflare Access 설정이 모두 맞은 것이다.
    """
    d = caller.device
    return {
        "user": caller.user.public(),
        "device": {"access_key": d.access_key, "name": d.name},
        "scopes": sorted(caller.scopes),
    }
