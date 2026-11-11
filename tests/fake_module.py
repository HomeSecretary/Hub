"""테스트용 가짜 모듈. 허브는 모듈 코드를 모르니, 계약(docs/module-contract.md)대로만 답하면 된다.

  demo 모듈
    send    승인 필요. to 가 @family.example 이 아니면 422
    ping    승인 없이 바로 실행
    secret  관리자(admin)만
  down 모듈: 항상 500 (꺼진 모듈 흉내)
"""
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

TOKEN = "d" * 40

MANIFEST = {
    "name": "demo",
    "title": "데모",
    "actions": {
        "send": {"title": "보내기", "approval": True},
        "ping": {"title": "핑"},
        "secret": {"title": "비밀", "roles": ["admin"]},
    },
}


def build():
    """가짜 모듈 앱과, 실행된 요청을 모아 두는 리스트를 돌려준다."""
    app = FastAPI()
    executed: list[dict] = []

    def authorized(request: Request) -> bool:
        return request.headers.get("Authorization") == f"Bearer {TOKEN}"

    @app.get("/demo/manifest")
    async def manifest(request: Request):
        if not authorized(request):
            return JSONResponse({"message": "unauthorized"}, 401)
        return MANIFEST

    @app.post("/demo/actions/{action}/validate")
    async def validate(action: str, request: Request):
        if not authorized(request):
            return JSONResponse({"message": "unauthorized"}, 401)
        body = await request.json()
        to = body["params"].get("to", "")
        if action == "send" and not to.endswith("@family.example"):
            return JSONResponse({"message": "허용되지 않은 수신자가 있습니다."}, 422)
        return {"summary": f"{action} → {to}"}

    @app.post("/demo/actions/{action}")
    async def execute(action: str, request: Request):
        if not authorized(request):
            return JSONResponse({"message": "unauthorized"}, 401)
        body = await request.json()
        if body["params"].get("boom"):
            return JSONResponse({"message": "boom"}, 500)
        executed.append({"action": action, **body})
        return {"result": f"{action} 완료"}

    @app.api_route("/down/{path:path}", methods=["GET", "POST"])
    async def down(path: str):
        return JSONResponse({"message": "down"}, 500)

    return app, executed
