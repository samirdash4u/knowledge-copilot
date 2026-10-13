"""HTTP API. The caller's role and tenant come from the bearer token only.

No ``from __future__ import annotations`` here: FastAPI resolves dependency annotations at runtime.
"""

from dataclasses import asdict
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from copilot.acl import AccessViolation
from copilot.auth import AuthError, issue_token, verify_token
from copilot.config import Settings, get_settings
from copilot.models import Principal
from copilot.service import CopilotService

bearer = HTTPBearer(auto_error=False)


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    mode: Literal["bm25", "vector", "hybrid", "hybrid_rerank"] | None = None
    use_cache: bool = True


class TokenRequest(BaseModel):
    user_id: str = "demo"
    role: Literal["employee", "manager", "admin"]
    tenant: str = "policy"


def create_app(settings: Settings | None = None, service: CopilotService | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="Knowledge Copilot", version="0.1.0")
    app.state.settings = settings
    app.state.service = service

    def get_service(request: Request) -> CopilotService:
        if request.app.state.service is None:
            request.app.state.service = CopilotService.from_settings(settings)
        return request.app.state.service

    def get_principal(
        creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> Principal:
        if creds is None:
            raise HTTPException(401, "missing bearer token")
        try:
            return verify_token(settings, creds.credentials)
        except AuthError as exc:
            raise HTTPException(401, "invalid token") from exc

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/whoami")
    def whoami(principal: Annotated[Principal, Depends(get_principal)]) -> dict:
        return {
            "user_id": principal.user_id,
            "role": principal.role.label,
            "tenant": principal.tenant,
        }

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
