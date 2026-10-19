"""Application assembly.

The service is reached only through the Pages Function, which forwards
``sgu-acw.zh314.xyz/api/*`` here. That keeps the browser on one origin, so
cookies need no ``SameSite=None`` and there is no CORS configuration at all.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import migrate, models
from . import notifications as notices
from .config import get_settings
from .db import get_engine, init_db, session_scope
from .deps import require_origin_secret
from .models import Role, User, utcnow
from .routers import (
    accounts,
    admin,
    auth,
    camps,
    daily,
    editorials,
    learning,
    meta,
    notifications,
    posts,
    practice,
    problemlists,
    push,
    signups,
    users,
    webauthn,
    workshop,
)
from .security import hash_password
from .student_id import ROOT_STUDENT_ID, StudentIdError, is_root_student_id, parse_student_id

logger = logging.getLogger("sgu_acw")

API_PREFIX = "/api"

#: 校验失败时不能回显原值的字段。密码尤其不能——Pydantic 会把出错的原值
#: 放进 ``input``，默认直接返回给客户端。
SENSITIVE_FIELDS = frozenset({"password", "current_password", "new_password", "token"})


def bootstrap_root() -> None:
    """Create the seed root account on first run.

    幂等性看的是「已经存在 root 角色的人」，而不是「这个学号存在」。

    这个区别很重要：如果按学号判断，一旦有人改了 root 的学号（比如从真实
    学号换成占位号），下次重启就会因为「这个学号不存在」而**再建一个
    root**。实际就这么踩过。

    root 的学号默认是十一个 0，不是真实学号——它不占用任何人的号，也不会
    跟人撞号。因此这里不跑学号解析，年级学院都填占位值。
    """
    settings = get_settings()
    if not settings.bootstrap_root_password:
        return

    with Session(get_engine()) as db:
        # 已经有 root 就什么都不做。这一条先判，后面才轮得到学号。
        if db.exec(select(User).where(User.role == Role.ROOT)).first() is not None:
            return

        student_id = (settings.bootstrap_root_student_id or ROOT_STUDENT_ID).strip()

        # 配了真实学号时仍然解析，方便从旧配置平滑过渡；但推荐用占位号。
        if is_root_student_id(student_id):
            parsed = None
        else:
            try:
                parsed = parse_student_id(student_id)
            except StudentIdError:
                logger.error(
                    "bootstrap root student id %r is not valid; falling back to %s",
                    student_id,
                    ROOT_STUDENT_ID,
                )
                student_id = ROOT_STUDENT_ID
                parsed = None

        # 学号可能被别的东西占了（比如配置指向一个已注册的成员），
        # 那就退回占位号，不要覆盖别人。
        if db.exec(select(User).where(User.student_id == student_id)).first() is not None:
            logger.warning(
                "bootstrap root student id %r is taken; using %s instead",
                student_id,
                ROOT_STUDENT_ID,
            )
            student_id = ROOT_STUDENT_ID
            parsed = None

        db.add(
            User(
                student_id=student_id,
                password_hash=hash_password(settings.bootstrap_root_password),
                display_name="root",
                enroll_year=parsed.enroll_year if parsed else 0,
                level=parsed.level if parsed else 0,
                college_code=parsed.college_code if parsed else "00",
                major_code=parsed.major_code if parsed else "00",
                class_no=parsed.class_no if parsed else 0,
                role=Role.ROOT,
                admin_note="首次启动自动创建",
                created_at=utcnow(),
            )
        )
        db.commit()
        logger.info("created bootstrap root account %s", student_id)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # 列级迁移跟启动绑死：部署忘了跑 python -m app.migrate 也不能让
    # 「no such column」炸到线上。
    migrate.run_migration()
    bootstrap_root()
    yield


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="ACW API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        redoc_url=None,
    )

    app.include_router(meta.health_router, prefix=API_PREFIX)
    app.include_router(meta.router, prefix=API_PREFIX)
    app.include_router(auth.router, prefix=API_PREFIX)
    app.include_router(users.router, prefix=API_PREFIX)
    app.include_router(accounts.router, prefix=API_PREFIX)
    app.include_router(practice.router, prefix=API_PREFIX)
    app.include_router(problemlists.router, prefix=API_PREFIX)
    app.include_router(daily.router, prefix=API_PREFIX)
    app.include_router(camps.router, prefix=API_PREFIX)
    app.include_router(posts.router, prefix=API_PREFIX)
    app.include_router(editorials.router, prefix=API_PREFIX)
    app.include_router(editorials.internal_router, prefix=API_PREFIX)
    app.include_router(admin.router, prefix=API_PREFIX)
    app.include_router(notifications.router, prefix=API_PREFIX)
    app.include_router(workshop.router, prefix=API_PREFIX)
    app.include_router(push.router, prefix=API_PREFIX)
    app.include_router(signups.router, prefix=API_PREFIX)
    app.include_router(webauthn.router, prefix=API_PREFIX)
    # 题库：题目/版本/提交记录。表由 init_db 的 create_all 建立。
    app.include_router(learning.router, prefix=API_PREFIX)

    def record_request_health(method: str, path: str, failed: bool) -> None:
        try:
            with session_scope() as db:
                key = f"api:{method}:{path}"
                if failed:
                    notices.report(db, key, "服务接口异常",
                                   f"{method} {path} 返回服务器错误，请查看服务日志。")
                else:
                    notices.resolve(db, key)
        except Exception:
            logger.exception("could not persist API health notification")

    @app.middleware("http")
    async def notify_server_errors(request, call_next):
        try:
            response = await call_next(request)
        except Exception:
            logger.exception("unhandled API request error")
            response = JSONResponse(status_code=500, content={"detail": "服务暂时异常，请稍后重试"})
        route = request.scope.get("route")
        # 使用路由模板，不记录查询参数、正文、密码或令牌。
        if route and (response.status_code >= 500 or response.status_code < 400):
            await run_in_threadpool(record_request_health, request.method, route.path,
                                    response.status_code >= 500)
        return response


    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(_request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_request, exc: RequestValidationError) -> JSONResponse:
        """把校验错误里的密码抹掉。

        Pydantic 校验失败时会把出错字段的**原值**放进 ``input``，而 FastAPI
        默认把它整个返回给客户端。密码超长或类型不对时，密码就这这样原样
        出现在响应体里：浏览器的 Network 面板、控制台、前端把它当提示显示
        出来，都是泄面。

        所以对标记为敏感字段的路径，把 ``input`` 换成占位符；其余错误照常
        返回，因为那些信息对用户有用（比如学号格式不对）。
        """
        details = []
        for error in exc.errors():
            item = dict(error)
            # loc 形如 ("body", "password")，最后一段是字段名。
            field = str(item.get("loc", ("",))[-1]) if item.get("loc") else ""
            if field in SENSITIVE_FIELDS and "input" in item:
                item["input"] = "***"
            # pydantic 的 ctx.error 是裸异常对象，直接进 JSONResponse 会 500。
            if isinstance(item.get("ctx"), dict):
                item["ctx"] = {
                    key: str(value) if isinstance(value, BaseException) else value
                    for key, value in item["ctx"].items()
                }
            details.append(item)
        return JSONResponse(status_code=422, content={"detail": details})

    @app.get(f"{API_PREFIX}/config", dependencies=[Depends(require_origin_secret)])
    def public_config() -> dict:
        """Small, non-sensitive constants the frontend needs."""
        return {
            "platforms": [
                {"value": p, "label": models.Platform.LABELS[p]} for p in models.Platform.ALL
            ],
            "cookie_name": settings.cookie_name,
        }

    return app


app = create_app()
