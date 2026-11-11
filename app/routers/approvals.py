"""승인 대기열 API: 목록, 승인(그리고 실행), 거절."""
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from ..auth.guard import Caller, require
from ..auth.scopes import APPROVALS_DECIDE
from ..approvals import APPROVED, FAILED, REJECTED, ApprovalError
from ..catalog import ModuleError, caller_context

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])
logger = logging.getLogger("secretary.approvals")

_ERROR_STATUS = {
    "not_found": status.HTTP_404_NOT_FOUND,
    "not_pending": status.HTTP_409_CONFLICT,
    "same_device": status.HTTP_403_FORBIDDEN,
}


def _raise(e: ApprovalError):
    """ApprovalError를 알맞은 HTTP 오류로 바꿔 던진다 (not_found → 404, not_pending → 409, same_device → 403)."""
    raise HTTPException(_ERROR_STATUS.get(e.code, 400), detail=e.message)


@router.get("")
async def list_approvals(
    request: Request,
    status_filter: str | None = Query("pending", alias="status", pattern="^(pending|approved|rejected|expired|failed)$"),
    caller: Caller = Depends(require(APPROVALS_DECIDE)),
):
    """내가 결정할 수 있는 요청만 보인다. 다른 가족의 요청은 보이지 않는다(아이 요청은 관리자에게 보인다)."""
    items = request.app.state.approvals.list_for(caller.user.id, caller.user.role, status_filter)
    return {"items": [a.public() for a in items]}


async def _decide(approval_id: str, request: Request, caller: Caller, decision: str):
    """승인/거절의 공통 부분. 대기열에서 상태를 한 번만 바꾸고(claim) 감사 로그를 남긴다.

    규칙에 어긋나면 이유를 로그에 남기고 HTTP 오류로 끝낸다.
    """
    state = request.app.state
    user, key = caller.user, caller.device.access_key
    try:
        approval = state.approvals.claim(approval_id, user.id, user.role, key, decision)
    except ApprovalError as e:
        state.audit.write("approval.denied", user=user.id, device=key, ip=caller.ip, approval=approval_id,
                          reason=e.code)
        _raise(e)
    event = "approval.approve" if decision == APPROVED else "approval.reject"
    state.audit.write(event, user=user.id, device=key, ip=caller.ip, approval=approval.id, action=approval.action,
                      requested_user=approval.requested_user)
    return approval


@router.post("/{approval_id}/approve")
async def approve(approval_id: str, request: Request, caller: Caller = Depends(require(APPROVALS_DECIDE))):
    """POST /v1/approvals/{id}/approve : 요청을 승인하고 바로 모듈에 실행을 맡긴다.

    실행이 실패하면 상태를 failed로 남긴다. 모듈이 "입력이 틀렸다"(422)고 하면 그 메시지를,
    그 밖의 실패는 502를 돌려준다. 자세한 원인은 서버 로그에만 남긴다.
    """
    state = request.app.state
    approval = await _decide(approval_id, request, caller, APPROVED)
    try:
        result = await _execute(state, approval)
    except ModuleError as e:
        logger.warning("approval %s execution failed: %s %s", approval.id, e.status, e.message)
        code, message = (422, e.message) if e.status == 422 else (502, "실행에 실패했습니다.")
    except Exception:
        logger.exception("approval %s execution failed", approval.id)
        code, message = 502, "실행에 실패했습니다."
    else:
        approval = state.approvals.set_result(approval.id, APPROVED, result)
        state.audit.write("approval.executed", user=caller.user.id, device=caller.device.access_key, ip=caller.ip,
                          approval=approval.id, action=approval.action)
        return {"approval": approval.public()}

    state.approvals.set_result(approval.id, FAILED, message)
    state.audit.write("approval.failed", user=caller.user.id, device=caller.device.access_key, ip=caller.ip,
                      approval=approval.id, action=approval.action)
    raise HTTPException(code, detail=message)


@router.post("/{approval_id}/reject")
async def reject(approval_id: str, request: Request, caller: Caller = Depends(require(APPROVALS_DECIDE))):
    """POST /v1/approvals/{id}/reject : 요청을 거절한다. 실행하지 않는다."""
    approval = await _decide(approval_id, request, caller, REJECTED)
    return {"approval": approval.public()}


async def _execute(state, approval) -> str:
    """승인된 요청을 모듈에 넘겨 실행하고 결과 문구를 돌려준다.

    허브는 payload 안의 params가 무슨 뜻인지 모른다. 요청할 때 받아 둔 그대로 모듈에 넘긴다.
    요청한 사람이 그사이 비활성화됐을 수 있으니 실행 직전에 한 번 더 확인한다.
    """
    requester = state.registry.user(approval.requested_user)
    if requester is None or requester.disabled:
        raise RuntimeError("requester disabled")
    p = approval.payload
    return await state.catalog.execute(p["module"], p["action"], p["params"], caller_context(requester))
