"""모든 모듈을 부르는 하나의 입구: POST /v1/commands, 그리고 쓸 수 있는 모듈 목록 GET /v1/modules.

모듈이 늘어나도 이 파일은 바뀌지 않는다. 클라이언트는 {"module", "action", "params"}만 보내면 된다.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..approvals import RULE_ADMIN
from ..auth.guard import Caller, authenticate, require
from ..auth.scopes import SYSTEM_READ
from ..catalog import ModuleError, caller_context

router = APIRouter(prefix="/v1", tags=["commands"])


class CommandRequest(BaseModel):
    """명령 요청 본문. params 안쪽 모양은 허브가 아니라 모듈이 검사한다."""

    module: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,23}$")
    action: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,31}$")
    params: dict = Field(default_factory=dict)


@router.post("/commands")
async def run_command(req: CommandRequest, request: Request, caller: Caller = Depends(authenticate)):
    """명령 하나를 처리한다.

    1. 권한: 기기와 역할이 "<모듈>:<액션>" scope를 허락하는지 (manifest의 roles도)
    2. 검사: 모듈에 입력을 검사받고 요약을 받는다
    3. 승인이 필요한 액션이면 대기열에 올리고 202, 아니면 바로 실행하고 200
    """
    state = request.app.state
    user, key = caller.user, caller.device.access_key
    scope = f"{req.module}:{req.action}"
    # 권한부터 본다. 권한이 없는 기기에는 그런 모듈이 있는지조차 알려 주지 않는다.
    if not caller.can(scope):
        raise caller.forbid(request, scope)
    try:
        spec = await state.catalog.action(req.module, req.action)
        if spec.roles is not None and user.role not in spec.roles:
            raise caller.forbid(request, scope)
        ctx = caller_context(user)
        summary = await state.catalog.validate(req.module, req.action, req.params, ctx)

        if spec.approval:
            approval = state.approvals.create(
                action=f"{req.module}.{req.action}",
                payload={"module": req.module, "action": req.action, "params": req.params},
                summary=f"{user.name}: {summary}",
                user_id=user.id, role=user.role, device_key=key,
            )
            state.audit.write("approval.create", user=user.id, device=key, ip=caller.ip,
                              approval=approval.id, action=approval.action, rule=approval.rule)
            who = "관리자 기기에서" if approval.rule == RULE_ADMIN else "다른 내 기기에서"
            return JSONResponse({"approval": approval.public(), "message": f"{who} 승인하면 실행됩니다."},
                                status.HTTP_202_ACCEPTED)

        result = await state.catalog.execute(req.module, req.action, req.params, ctx)
    except ModuleError as e:
        state.audit.write("command.fail", user=user.id, device=key, ip=caller.ip, action=scope, status=e.status)
        raise HTTPException(e.status, detail=e.message)
    state.audit.write("command.execute", user=user.id, device=key, ip=caller.ip, action=scope)
    return {"result": result}


@router.get("/modules")
async def list_modules(request: Request, caller: Caller = Depends(require(SYSTEM_READ))):
    """이 기기로 쓸 수 있는 모듈과 액션 목록. 앱이나 안경이 메뉴를 그릴 때 쓴다.

    꺼져 있는 모듈은 available: false 로 보여 준다. 권한이 없는 액션은 목록에서 뺀다.
    """
    catalog = request.app.state.catalog
    items = []
    for name in catalog.endpoints:
        try:
            manifest = await catalog.manifest(name)
        except ModuleError:
            items.append({"name": name, "available": False, "actions": []})
            continue
        actions = [
            {"name": a, "title": spec.title, "approval": spec.approval, "params": spec.params}
            for a, spec in manifest.actions.items()
            if caller.can(f"{name}:{a}") and (spec.roles is None or caller.user.role in spec.roles)
        ]
        if actions:
            items.append({"name": name, "title": manifest.title, "available": True, "actions": actions})
    return {"items": items}
