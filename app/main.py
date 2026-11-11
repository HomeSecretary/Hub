import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from .audit import AuditLog
from .auth.registry import Registry
from .config import Settings, get_settings
from .db import Database
from .approvals import ApprovalQueue
from .catalog import ModuleCatalog
from .events import EventStore
from .routers import approvals, commands, events, system

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("secretary")


def create_app(settings: Settings | None = None, module_transport=None) -> FastAPI:
    """FastAPI 앱을 만든다. 테스트에서는 settings와 가짜 모듈(module_transport)을 넣어 따로 만든다.

    운영 서버는 맨 아래 app = create_app() 을 uvicorn이 불러 쓴다.
    """
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        """서버가 켜질 때(yield 전) 필요한 객체를 만들어 app.state에 두고, 꺼질 때(yield 뒤) 정리한다.

        라우터는 request.app.state.db 처럼 꺼내 쓴다.
        """
        app.state.settings = settings
        app.state.db = Database(settings.db_path)
        app.state.registry = Registry(settings.registry_path, settings.max_users)
        app.state.audit = AuditLog(settings.audit_path)
        app.state.approvals = ApprovalQueue(app.state.db, settings.approval_ttl_seconds)
        app.state.events = EventStore(app.state.db)
        app.state.catalog = ModuleCatalog(settings.module_endpoints, settings.module_timeout_seconds,
                                          transport=module_transport)
        app.state.audit.write("server.start", modules=sorted(settings.module_endpoints))
        yield
        await app.state.catalog.close()
        app.state.db.close()

    # 운영에서는 문서 페이지를 열지 않는다. 스펙이 필요하면 로컬에서 따로 뽑는다.
    app = FastAPI(title="Secretary", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def request_id(request: Request, call_next):
        """모든 응답에 X-Request-Id를 붙이는 미들웨어.

        예상 못 한 오류가 나면 자세한 내용은 서버 로그에만 남기고, 클라이언트에는 같은 ID가 붙은 500만 돌려준다.
        사용자가 ID를 알려 주면 로그에서 그 요청을 바로 찾을 수 있다.
        """
        rid = uuid.uuid4().hex[:12]
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled error rid=%s path=%s", rid, request.url.path)
            response = JSONResponse({"code": "INTERNAL_ERROR", "message": "일시적인 오류가 발생했습니다."}, 500)
        response.headers["X-Request-Id"] = rid
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        """HTTP 오류(401, 403, 404 ...)를 {code, message} 모양의 JSON으로 통일한다."""
        return JSONResponse({"code": f"HTTP_{exc.status_code}", "message": exc.detail}, exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        """요청 형식 오류(422)를 {code, message, fields} 모양으로 바꾼다."""
        # 어떤 필드가 왜 틀렸는지만 알려 주고, 입력값 자체는 돌려주지 않는다.
        fields = [".".join(str(p) for p in e["loc"][1:]) for e in exc.errors()]
        return JSONResponse({"code": "INVALID_REQUEST", "message": "요청 형식이 올바르지 않습니다.", "fields": fields}, 422)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        """GET /healthz : 서버가 살아 있는지. 인증 없이 열려 있고, 컨테이너 상태 확인(healthcheck)이 부른다."""
        return {"ok": True}

    app.include_router(system.router)
    app.include_router(commands.router)
    app.include_router(events.router)
    app.include_router(events.internal)
    app.include_router(approvals.router)
    return app


app = create_app()
