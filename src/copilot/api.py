"""HTTP API and web UI. The caller's role and tenant come from the bearer token only.

No ``from __future__ import annotations`` here: FastAPI resolves dependency annotations at runtime.
"""

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from copilot.acl import AccessViolation
from copilot.auth import AuthError, decode_token, issue_token
from copilot.config import Settings, get_settings
from copilot.models import Principal
from copilot.service import CopilotService
from copilot.users import LoginThrottle, PasswordPolicyError, User, UserStore

log = logging.getLogger(__name__)
bearer = HTTPBearer(auto_error=False)
WEB_DIR = Path(__file__).parent / "web"


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal["bm25", "vector", "hybrid", "hybrid_rerank"] | None = None
    use_cache: bool = True


class TokenRequest(BaseModel):
    user_id: str = "demo"
    role: Literal["employee", "manager", "admin"]
    tenant: str = "policy"


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    tenant: str | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=1, max_length=256)


class SwitchTenantRequest(BaseModel):
    tenant: str


@dataclass
class Caller:
    principal: Principal
    user: User | None  # None for service tokens (CLI / dev endpoint)


def _session(settings: Settings, user: User, tenant: str) -> dict:
    token = issue_token(
        settings,
        user.username,
        user.role,
        tenant,
        extra={"src": "user", "ver": user.token_version},
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.jwt_ttl_minutes * 60,
        "user": _me(user, tenant),
    }


def _me(user: User, tenant: str) -> dict:
    return {
        "username": user.username,
        "role": user.role.label,
        "tenant": tenant,
        "tenants": user.tenants,
        "must_change_password": user.must_change,
    }


def create_app(
    settings: Settings | None = None,
    service: CopilotService | None = None,
    users: UserStore | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Knowledge Copilot", version="0.2.0")
    app.state.settings = settings
    app.state.service = service

    if users is None:
        users = UserStore(settings.users_db, settings.password_hash_iterations)
    if settings.seed_demo_users:
        created = users.seed_demo_users(settings.demo_password)
        if created:
            log.warning(
                "created demo users %s with the default password; each must change it at"
                " first login",
                ", ".join(created),
            )
    app.state.users = users
    throttle = LoginThrottle(settings.login_max_failures, settings.login_lockout_s)

    def get_service(request: Request) -> CopilotService:
        if request.app.state.service is None:
            request.app.state.service = CopilotService.from_settings(settings)
        return request.app.state.service

    def get_caller(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Caller:
        if creds is None:
            raise HTTPException(401, "missing bearer token")
        try:
            principal, claims = decode_token(settings, creds.credentials)
        except AuthError as exc:
            raise HTTPException(401, "invalid token") from exc
        if claims.get("src") != "user":
            return Caller(principal, None)
        # Tokens from a login are re-checked against the database, so a password change,
        # a role change or a removed tenant takes effect immediately.
        user = users.get(principal.user_id)
        if (
            user is None
            or claims.get("ver") != user.token_version
            or user.role != principal.role
            or principal.tenant not in user.tenants
        ):
            raise HTTPException(401, "session expired, please sign in again")
        return Caller(principal, user)

    def get_principal(caller: Annotated[Caller, Depends(get_caller)]) -> Principal:
        if caller.user is not None and caller.user.must_change:
            raise HTTPException(403, "password change required")
        return caller.principal

    # ---------- web UI ----------

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    # ---------- auth ----------

    @app.post("/auth/login")
    def login(body: LoginRequest) -> dict:
        username = body.username.strip().lower()
        if throttle.locked(username):
            raise HTTPException(429, "too many failed attempts, try again later")
        user = users.authenticate(username, body.password)
        if user is None:
            throttle.fail(username)
            raise HTTPException(401, "invalid username or password")
        throttle.reset(username)
        tenant = body.tenant or user.tenants[0]
        if tenant not in user.tenants:
            raise HTTPException(403, f"no access to tenant {tenant!r}")
        return _session(settings, user, tenant)

    @app.post("/auth/change-password")
    def change_password(
        body: ChangePasswordRequest, caller: Annotated[Caller, Depends(get_caller)]
    ) -> dict:
        if caller.user is None:
            raise HTTPException(400, "this token is not tied to a user account")
        try:
            user = users.change_password(
                caller.user.username, body.current_password, body.new_password
            )
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except PasswordPolicyError as exc:
            raise HTTPException(422, str(exc)) from exc
        # Older tokens are now invalid; hand back a fresh one.
        return _session(settings, user, caller.principal.tenant)

    @app.post("/auth/switch-tenant")
    def switch_tenant(
        body: SwitchTenantRequest, caller: Annotated[Caller, Depends(get_caller)]
    ) -> dict:
        if caller.user is None:
            raise HTTPException(400, "this token is not tied to a user account")
        if body.tenant not in caller.user.tenants:
            raise HTTPException(403, f"no access to tenant {body.tenant!r}")
        return _session(settings, caller.user, body.tenant)

    @app.get("/me")
    def me(caller: Annotated[Caller, Depends(get_caller)]) -> dict:
        if caller.user is None:
            p = caller.principal
            return {
                "username": p.user_id,
                "role": p.role.label,
                "tenant": p.tenant,
                "tenants": [p.tenant],
                "must_change_password": False,
            }
        return _me(caller.user, caller.principal.tenant)

    @app.get("/whoami")
    def whoami(principal: Annotated[Principal, Depends(get_principal)]) -> dict:
        return {
            "user_id": principal.user_id,
            "role": principal.role.label,
            "tenant": principal.tenant,
        }

    # ---------- question answering ----------

    @app.post("/ask")
    def ask(
        body: AskRequest,
        principal: Annotated[Principal, Depends(get_principal)],
        service: Annotated[CopilotService, Depends(get_service)],
    ) -> dict:
        try:
            ans = service.answer(principal, body.question, mode=body.mode, use_cache=body.use_cache)
        except AccessViolation as exc:
            # Fail closed: never return partial results after an ACL check fails.
            raise HTTPException(500, "internal access check failed") from exc
        return asdict(ans)

    if settings.dev_token_endpoint:

        @app.post("/auth/dev-token")
        def dev_token(body: TokenRequest) -> dict:
            return {
                "access_token": issue_token(settings, body.user_id, body.role, body.tenant),
                "token_type": "bearer",
            }

    return app


def app_factory() -> FastAPI:
    return create_app()
