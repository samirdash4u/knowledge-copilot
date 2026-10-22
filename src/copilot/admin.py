"""Admin console and document upload endpoints for the web UI.

Who may do what is decided here, on the server, from the signed-in user (never from the request
body):

- every signed-in user: upload a document to a tenant they can use, with a minimum role no higher
  than their own; list the documents of such a tenant that their role can see; start an ingest;
  follow ingest jobs and their own jobs.
- admins, in addition: status, tenants, users and tenant grants, eval runs and results, model
  download, deleting documents, and every job.

No ``from __future__ import annotations`` here: FastAPI resolves dependency annotations at runtime.
"""

import json
import logging
import os
import re
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from copilot.config import Settings
from copilot.eval.runner import evaluate, load_questions, sample_questions
from copilot.ingest.convert import CONVERTIBLE, existing_doc_ids, import_document
from copilot.ingest.loaders import _slug, load_file
from copilot.ingest.pipeline import corpus_files, pending_changes, read_manifest
from copilot.jobs import Job, JobContext, JobManager
from copilot.models import Principal, Role
from copilot.retrieval.hybrid import MODES
from copilot.service import CopilotService, IndexMissing, ingest, make_embedder
from copilot.tenants import TENANT_FILE, check_roles, create_tenant, list_tenants, write_meta
from copilot.users import PasswordPolicyError, User, UserStore, check_password_policy

log = logging.getLogger(__name__)

# Jobs anyone signed in may watch, whoever started them; others see only their own. (A suggest
# job's label names a document, which may be one their role can't see.)
SHARED_JOB_KINDS = ("ingest",)
SAFE_FILENAME = re.compile(r"[^A-Za-z0-9._ -]+")


@dataclass
class Account:
    """The signed-in caller as the admin endpoints see it."""

    principal: Principal
    user: User | None  # None for service tokens (``copilot token``)

    @property
    def name(self) -> str:
        return self.principal.user_id

    @property
    def role(self) -> Role:
        return self.principal.role

    @property
    def is_admin(self) -> bool:
        return self.principal.role == Role.ADMIN

    @property
    def tenants(self) -> list[str]:
        return self.user.tenants if self.user is not None else [self.principal.tenant]


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    label: str | None = Field(default=None, max_length=80)
    roles: list[str] = []
    users: list[str] = []


class TenantUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=80)
    roles: list[str] | None = None


class UserCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    role: str
    tenants: list[str] = Field(min_length=1)
    must_change: bool = True


class UserUpdate(BaseModel):
    role: str | None = None
    tenants: list[str] | None = None


class PasswordReset(BaseModel):
    password: str = Field(min_length=1, max_length=256)


class EvalRequest(BaseModel):
    limit: int | None = Field(default=None, ge=1, le=10_000)
    judge: bool = False
    retrieval_only: bool = False


def match_owner(path: Path) -> None:
    """Give a file the API created the owner of its folder.

    In Docker the API runs as root and data/corpus is mounted from the host, so without this a
    document uploaded in the browser would belong to root on a Linux host. A no-op on Windows and
    when not running as root.
    """
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        return
    try:
        st = path.parent.stat()
        os.chown(path, st.st_uid, st.st_gid)
    except OSError as exc:
        log.warning("could not set the owner of %s: %s", path, exc)


def _role(value: str) -> Role:
    try:
        return Role.parse(value)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _check(component: str, fn: Callable[[], str]) -> dict:
    try:
        return {"name": component, "ok": True, "detail": fn()}
    except Exception as exc:  # noqa: BLE001 - any failure is shown as "not reachable"
        return {"name": component, "ok": False, "detail": str(exc) or type(exc).__name__}


def install(
    app: FastAPI,
    settings: Settings,
    users: UserStore,
    get_caller: Callable,
    load_service: Callable[[], CopilotService],
) -> JobManager:
    """Add the admin console and upload routes to ``app``. Returns the job manager."""
    jobs = JobManager()
    app.state.jobs = jobs

    def get_account(caller: Annotated[object, Depends(get_caller)]) -> Account:
        if caller.user is not None and caller.user.must_change:
            raise HTTPException(403, "password change required")
        return Account(caller.principal, caller.user)

    def get_admin(account: Annotated[Account, Depends(get_account)]) -> Account:
        if not account.is_admin:
            raise HTTPException(403, "only admins can do this")
        return account

    def known_tenants() -> dict[str, object]:
        return {t.name: t for t in list_tenants(settings.corpus_dir)}

    def tenant_dir(name: str) -> Path:
        folder = settings.corpus_dir / name
        if name not in known_tenants():
            raise HTTPException(404, f"no tenant {name!r}")
        return folder

    def require_tenant(account: Account, tenant: str) -> Path:
        if tenant not in account.tenants:
            raise HTTPException(403, f"you don't have access to tenant {tenant!r}")
        return tenant_dir(tenant)

    def can_see_job(account: Account, job: Job) -> bool:
        return account.is_admin or job.started_by == account.name or job.kind in SHARED_JOB_KINDS

    def job_view(account: Account, job: Job) -> dict:
        return job.to_dict(details=account.is_admin or job.started_by == account.name)

    # ------------------------------------------------------------------ jobs

    def ingest_job(ctx: JobContext) -> dict:
        try:
            previous = load_service()
        except IndexMissing:
            previous = None
        embedder = previous.retriever.embedder if previous else make_embedder(settings)
        manifest = ingest(settings, embedder, progress=ctx.progress)
        # From here on the new index is on disk; finish even if someone presses Cancel.
        ctx.log("Loading the new index into the running server")
        app.state.service = CopilotService.from_settings(
            settings, previous=previous, embedder=embedder
        )
        ctx.log(f"Indexed {manifest['documents']} documents ({manifest['chunks']} chunks)")
        return {
            k: manifest[k]
            for k in ("documents", "chunks", "by_tenant", "seconds", "corpus_version")
        }

    def queue_ingest(account: Account) -> tuple[Job, bool]:
        return jobs.submit("ingest", "Index documents", ingest_job, account.name, reuse_queued=True)

    def suggest_job(doc_path: Path, tenant: str) -> Callable[[JobContext], dict]:
        def run(ctx: JobContext) -> dict:
            from copilot.ingest.loaders import load_markdown
            from copilot.suggestions import add_to_tenant, document_questions

            llm = None
            if settings.llm != "echo":
                try:
                    llm = load_service().llm
                except IndexMissing:
                    from copilot.service import make_llm

                    llm = make_llm(settings)
            ctx.log(f"Writing suggested questions for {doc_path.name}")
            by_role, used_llm = document_questions(load_markdown(doc_path, tenant), llm)
            folder = settings.corpus_dir / tenant
            existed = (folder / TENANT_FILE).exists()
            written = bool(by_role) and add_to_tenant(folder, by_role)
            if written and not existed:
                match_owner(folder / TENANT_FILE)
            return {"questions": by_role, "written": written, "used_llm": used_llm}

        return run

    def eval_job(body: EvalRequest) -> Callable[[JobContext], dict]:
        def run(ctx: JobContext) -> dict:
            service = load_service()
            questions = load_questions(settings.eval_questions)
            if body.limit:
                questions = sample_questions(questions, body.limit)
            modes = list(MODES)
            answer_modes = [] if body.retrieval_only else [settings.retrieval_mode]
            judge = service.llm if body.judge and answer_modes else None
            ctx.log(
                f"{len(questions)} questions; retrieval scored in {', '.join(modes)}"
                + (f"; LLM answers in {', '.join(answer_modes)}" if answer_modes else "")
            )
            ctx.progress(0, len(questions) * len(answer_modes), "Scoring retrieval")
            report = evaluate(
                service,
                questions,
                service.chunks,
                modes,
                judge=judge,
                answer_modes=answer_modes,
                progress=lambda done, total, label: ctx.progress(done, total, f"Answered {label}"),
            )
            out = settings.eval_report
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(report, indent=2), encoding="utf-8")
            match_owner(out)
            ctx.log(f"Report written to {out}")
            return eval_summary(report)

        return run

    def pull_job(ctx: JobContext) -> dict:
        model = settings.ollama_model
        ctx.log(f"Downloading {model} into Ollama")
        timeout = httpx.Timeout(30.0, read=None)
        url = f"{settings.ollama_url}/api/pull"
        with httpx.stream("POST", url, json={"model": model}, timeout=timeout) as r:
            r.raise_for_status()
            last = ""
            for line in r.iter_lines():
                if not line.strip():
                    continue
                msg = json.loads(line)
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                status = msg.get("status", "")
                if status != last and not msg.get("total"):
                    ctx.log(status)
                last = status
                ctx.progress(msg.get("completed") or 0, msg.get("total") or 0, status)
        return {"model": model}

    @app.post("/jobs/ingest")
    def start_ingest(account: Annotated[Account, Depends(get_account)]) -> dict:
        job, created = queue_ingest(account)
        return {**job_view(account, job), "created": created}

    @app.post("/jobs/eval")
    def start_eval(body: EvalRequest, account: Annotated[Account, Depends(get_admin)]) -> dict:
        if not settings.eval_questions.is_file():
            raise HTTPException(404, f"no eval questions at {settings.eval_questions}")
        size = f"{body.limit} questions" if body.limit else "all questions"
        kind = "retrieval only" if body.retrieval_only else "with answers"
        label = f"Evaluation ({size}, {kind}{', LLM judge' if body.judge else ''})"
        job, _ = jobs.submit("eval", label, eval_job(body), account.name, body.model_dump())
        return job_view(account, job)

    @app.post("/jobs/pull-model")
    def start_pull(account: Annotated[Account, Depends(get_admin)]) -> dict:
        if settings.llm == "echo":
            raise HTTPException(400, "the LLM is in test mode (COPILOT_LLM=echo); nothing to pull")
        job, _ = jobs.submit(
            "pull-model", f"Download {settings.ollama_model}", pull_job, account.name
        )
        return job_view(account, job)

    @app.get("/jobs")
    def list_jobs(account: Annotated[Account, Depends(get_account)]) -> list[dict]:
        return [job_view(account, j) for j in jobs.list() if can_see_job(account, j)]

    @app.get("/jobs/{job_id}")
    def get_job(job_id: str, account: Annotated[Account, Depends(get_account)]) -> dict:
        job = jobs.get(job_id)
        if job is None or not can_see_job(account, job):
            raise HTTPException(404, "no such job")
        return job_view(account, job)

    @app.post("/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, account: Annotated[Account, Depends(get_account)]) -> dict:
        job = jobs.get(job_id)
        if job is None or not can_see_job(account, job):
            raise HTTPException(404, "no such job")
        if not (account.is_admin or job.started_by == account.name):
            raise HTTPException(403, "only an admin or the person who started it can cancel it")
        return job_view(account, jobs.cancel(job_id))

    # ------------------------------------------------------------------ documents

    def describe(path: Path, tenant: str, built_at: float | None) -> dict | None:
        try:
            doc = load_file(path, tenant)
        except Exception as exc:  # noqa: BLE001 - one bad file must not hide the rest
            log.warning("cannot read %s: %s", path, exc)
            return None
        mtime = path.stat().st_mtime
        return {
            "doc_id": doc.doc_id,
            "title": doc.title,
            "min_role": doc.min_role.label,
            "file": path.relative_to(settings.corpus_dir / tenant).as_posix(),
            "sections": len(doc.sections),
            "updated_at": mtime,
            "indexed": built_at is not None and mtime <= built_at,
            "_role": doc.min_role,
        }

    @app.get("/documents")
    def documents(
        account: Annotated[Account, Depends(get_account)], tenant: str | None = None
    ) -> dict:
        """The tenant's documents this caller's role can see, and whether each is indexed."""
        tenant = tenant or account.principal.tenant
        folder = require_tenant(account, tenant)
        manifest = read_manifest(settings.index_dir)
        built_at = manifest.get("built_at") if manifest else None
        docs = []
        for path in corpus_files(folder):
            d = describe(path, tenant, built_at)
            if d is not None and account.role >= d.pop("_role"):
                docs.append(d)
        prefix = f"{tenant}/"
        return {
            "tenant": tenant,
            "documents": docs,
            "index_built_at": built_at,
            "pending": sum(
                1
                for p in pending_changes(settings, manifest)
                if p.startswith(prefix) or p.startswith("(")
            ),
            "upload_roles": [r.label for r in Role if r <= account.role],
            "upload_types": sorted(CONVERTIBLE),
            "max_upload_mb": settings.max_upload_mb,
            "active_ingest": [job_view(account, j) for j in jobs.active("ingest")],
        }

    @app.post("/documents")
    async def upload(
        request: Request,
        account: Annotated[Account, Depends(get_account)],
        tenant: str,
        min_role: str,
        filename: str,
        title: str | None = None,
        doc_id: str | None = None,
        overwrite: bool = False,
        suggest: bool = True,
        index: bool = False,
    ) -> dict:
        """Add a document. The request body is the file itself (PDF, DOCX, Markdown or text)."""
        folder = require_tenant(account, tenant)
        role = _role(min_role)
        if role > account.role:
            raise HTTPException(
                403,
                f"you can only upload documents your own role can see (up to {account.role.label})",
            )
        name = SAFE_FILENAME.sub("_", Path(filename.replace("\\", "/")).name).strip(" .")
        suffix = Path(name).suffix.lower()
        if not name or suffix not in CONVERTIBLE:
            raise HTTPException(
                415, f"unsupported file type; upload one of {', '.join(sorted(CONVERTIBLE))}"
            )
        limit = int(settings.max_upload_mb * 1024 * 1024)
        if int(request.headers.get("content-length") or 0) > limit:
            raise HTTPException(413, f"the file is larger than {settings.max_upload_mb:g} MB")
        data = bytearray()
        async for part in request.stream():
            data += part
            if len(data) > limit:
                raise HTTPException(413, f"the file is larger than {settings.max_upload_mb:g} MB")
        if not data:
            raise HTTPException(422, "the file is empty")

        def save() -> Path:
            final_id = _slug(doc_id or Path(name).stem)
            existing = existing_doc_ids(folder).get(final_id)
            if existing is not None:
                if not overwrite:
                    raise HTTPException(
                        409,
                        f"a document with id {final_id!r} already exists in {tenant}; tick"
                        " 'replace' to overwrite it or choose another document id",
                    )
                if load_file(existing, tenant).min_role > account.role:
                    # Don't let someone replace (and so read or wipe) a document they can't see.
                    raise HTTPException(403, f"you can't replace document {final_id!r}")
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                src = Path(tmp) / name
                src.write_bytes(bytes(data))
                try:
                    out = import_document(
                        src,
                        settings.corpus_dir,
                        tenant,
                        role,
                        title=title or None,
                        doc_id=final_id,
                        source_label=name,
                        overwrite=overwrite,
                    )
                except FileExistsError as exc:
                    raise HTTPException(409, str(exc)) from exc
                except (ValueError, RuntimeError) as exc:
                    raise HTTPException(422, f"could not read {name}: {exc}") from exc
                except Exception as exc:  # noqa: BLE001 - a broken PDF/DOCX raises anything
                    raise HTTPException(422, f"could not read {name}: {exc}") from exc
            match_owner(out)
            return out

        out = await run_in_threadpool(save)
        doc = load_file(out, tenant)
        queued = []
        if suggest:
            job, _ = jobs.submit(
                "suggest", f"Suggested questions for {doc.title}", suggest_job(out, tenant),
                account.name, {"tenant": tenant, "doc_id": doc.doc_id},
            )  # fmt: skip
            queued.append(job_view(account, job))
        if index:
            queued.append(job_view(account, queue_ingest(account)[0]))
        log.info("%s uploaded %s to %s (%s)", account.name, doc.doc_id, tenant, role.label)
        return {
            "tenant": tenant,
            "doc_id": doc.doc_id,
            "title": doc.title,
            "min_role": doc.min_role.label,
            "file": out.name,
            "sections": len(doc.sections),
            "jobs": queued,
        }

    @app.delete("/documents/{tenant}/{doc_id}")
    def delete_document(
        tenant: str, doc_id: str, account: Annotated[Account, Depends(get_admin)]
    ) -> dict:
        folder = tenant_dir(tenant)
        path = existing_doc_ids(folder).get(doc_id)
        if path is None:
            raise HTTPException(404, f"no document {doc_id!r} in {tenant}")
        path.unlink()
        sidecar = path.with_name(path.name + ".meta.yaml")
        if sidecar.exists():
            sidecar.unlink()
        log.info("%s deleted %s/%s", account.name, tenant, doc_id)
        return {"deleted": doc_id, "tenant": tenant}

    # ------------------------------------------------------------------ status

    def check_llm() -> str:
        if settings.llm == "echo":
            return "test mode (echo LLM), no model needed"
        r = httpx.get(f"{settings.ollama_url}/api/tags", timeout=3)
        r.raise_for_status()
        names = {m.get("name", "") for m in r.json().get("models", [])}
        want = settings.ollama_model
        if want in names or f"{want}:latest" in names:
            return f"Ollama is up and {want} is downloaded"
        raise RuntimeError(f"Ollama is up but {want} is not downloaded yet; use Download model")

    def check_vectors() -> str:
        if settings.vector_store == "memory":
            return "in-memory vector store"
        r = httpx.get(f"{settings.qdrant_url}/collections", timeout=3)
        r.raise_for_status()
        return f"Qdrant is up at {settings.qdrant_url}"

    def check_cache() -> str:
        if settings.cache != "redis":
            return f"{settings.cache} cache"
        import redis

        redis.Redis.from_url(settings.redis_url, socket_timeout=2).ping()
        return f"Redis is up at {settings.redis_url}"

    @app.get("/admin/status")
    def status(account: Annotated[Account, Depends(get_admin)]) -> dict:
        manifest = read_manifest(settings.index_dir)
        pending = pending_changes(settings, manifest)
        report = None
        if settings.eval_report.is_file():
            try:
                r = json.loads(settings.eval_report.read_text(encoding="utf-8"))
                report = {"generated_at": r.get("generated_at"), "questions": r.get("questions")}
            except ValueError:
                pass
        return {
            "index": {
                "built": manifest is not None,
                "documents": manifest.get("documents") if manifest else 0,
                "chunks": manifest.get("chunks") if manifest else 0,
                "by_tenant": manifest.get("by_tenant", {}) if manifest else {},
                "built_at": manifest.get("built_at") if manifest else None,
                "pending": pending[:50],
                "pending_count": len(pending),
            },
            "components": [
                _check("LLM", check_llm),
                _check("Vector store", check_vectors),
                _check("Answer cache", check_cache),
            ],
            "settings": {
                "llm": settings.ollama_model if settings.llm == "ollama" else settings.llm,
                "retrieval_mode": settings.retrieval_mode,
                "embedder": settings.embedding_model
                if settings.embedder == "sentence-transformers"
                else settings.embedder,
                "vector_store": settings.vector_store,
                "cache": settings.cache,
                "corpus_dir": str(settings.corpus_dir),
                "index_dir": str(settings.index_dir),
            },
            "can_pull_model": settings.llm == "ollama",
            "users": users.count(),
            "tenants": len(known_tenants()),
            "eval": report,
            "jobs": [job_view(account, j) for j in jobs.active()],
        }

    # ------------------------------------------------------------------ eval results

    def eval_summary(report: dict) -> dict:
        modes = {
            mode: {k: v for k, v in m.items() if k != "per_question"}
            for mode, m in report.get("modes", {}).items()
        }
        leaks = sum(m.get("leaks", 0) for m in modes.values())
        leaks += report.get("cache_poisoning_leaks", 0)
        unauthorized = sum(m.get("unauthorized", 0) for m in modes.values())
        return {
            "generated_at": report.get("generated_at"),
            "questions": report.get("questions"),
            "k": report.get("k"),
            "modes": modes,
            "cache_poisoning_leaks": report.get("cache_poisoning_leaks"),
            "leak_details": report.get("leak_details", []),
            "passed": leaks == 0 and unauthorized == 0,
        }

    @app.get("/admin/eval")
    def eval_results(account: Annotated[Account, Depends(get_admin)]) -> dict:
        """The latest eval report: a row per mode, and the answers that did not pass."""
        if not settings.eval_report.is_file():
            return {"report": None}
        try:
            report = json.loads(settings.eval_report.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise HTTPException(500, f"the eval report is not valid JSON: {exc}") from exc
        questions = {}
        if settings.eval_questions.is_file():
            questions = {q.id: q for q in load_questions(settings.eval_questions)}
        failures = []
        for mode, m in report.get("modes", {}).items():
            for row in m.get("per_question", []):
                bad = row.get("success") is False or row.get("leaked")
                if bad or row.get("unauthorized_chunks"):
                    q = questions.get(row["id"])
                    if row.get("leaked") or row.get("unauthorized_chunks"):
                        problem = "retrieved or leaked restricted content"
                    elif q and q.expect_refusal and not row.get("refused"):
                        problem = "should have refused"
                    elif row.get("refused"):
                        problem = "refused, but the answer is in the documents"
                    elif row.get("grounded") is False:
                        problem = "not grounded in its sources"
                    else:
                        problem = "missing expected facts"
                    failures.append(
                        {
                            "mode": mode,
                            "id": row["id"],
                            "type": row.get("type"),
                            "role": row.get("role"),
                            "question": q.question if q else "",
                            "problem": problem,
                            "answer": (row.get("answer") or "")[:400],
                            "refused": row.get("refused"),
                            "grounded": row.get("grounded"),
                            "leaked": row.get("leaked", []),
                        }
                    )
        return {"report": eval_summary(report), "failures": failures[:200]}

    # ------------------------------------------------------------------ tenants

    def tenant_users(name: str, roles: list[str], all_users: list[User]) -> list[str]:
        return [u.username for u in all_users if name in u.tenants or u.role.label in roles]

    @app.get("/admin/tenants")
    def admin_tenants(account: Annotated[Account, Depends(get_admin)]) -> list[dict]:
        all_users = users.list()
        return [
            {
                "name": t.name,
                "label": t.label,
                "documents": t.documents,
                "roles": t.roles,
                "users": tenant_users(t.name, t.roles, all_users),
            }
            for t in list_tenants(settings.corpus_dir)
        ]

    @app.post("/admin/tenants")
    def add_tenant(body: TenantCreate, account: Annotated[Account, Depends(get_admin)]) -> dict:
        try:
            roles = check_roles([r.strip().lower() for r in body.roles if r.strip()])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        names = [u.strip().lower() for u in body.users if u.strip()]
        missing = [n for n in names if users.get(n) is None]
        if missing:
            raise HTTPException(422, f"no such user: {', '.join(missing)}")
        try:
            tenant = create_tenant(
                settings.corpus_dir, body.name.strip().lower(), body.label, roles
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(409, f"tenant {body.name!r} already exists") from exc
        folder = settings.corpus_dir / tenant.name
        match_owner(folder)
        match_owner(folder / TENANT_FILE)
        for n in names:
            user = users.get(n)
            users.set_tenants(user.username, user.tenants + [tenant.name])
        log.info("%s created tenant %s", account.name, tenant.name)
        return {"name": tenant.name, "label": tenant.label, "roles": tenant.roles, "users": names}

    @app.patch("/admin/tenants/{name}")
    def update_tenant(
        name: str, body: TenantUpdate, account: Annotated[Account, Depends(get_admin)]
    ) -> dict:
        tenant = known_tenants().get(name)
        if tenant is None:
            raise HTTPException(404, f"no tenant {name!r}")
        label = (body.label or "").strip() or tenant.label
        roles = tenant.roles
        if body.roles is not None:
            try:
                roles = check_roles([r.strip().lower() for r in body.roles if r.strip()])
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        write_meta(settings.corpus_dir / name, label, roles)
        return {"name": name, "label": label, "roles": roles}

    # ------------------------------------------------------------------ users

    def user_view(user: User, tenants: list) -> dict:
        via_role = [t.name for t in tenants if user.role.label in t.roles]
        return {
            "username": user.username,
            "role": user.role.label,
            "tenants": user.tenants,
            "via_role": [t for t in via_role if t not in user.tenants],
            "must_change_password": user.must_change,
        }

    def check_tenant_names(names: list[str]) -> list[str]:
        names = list(dict.fromkeys(n.strip().lower() for n in names if n.strip()))
        unknown = [n for n in names if n not in known_tenants()]
        if unknown:
            raise HTTPException(422, f"no such tenant: {', '.join(unknown)}")
        if not names:
            raise HTTPException(422, "a user needs at least one tenant")
        return names

    def admins() -> list[str]:
        return [u.username for u in users.list() if u.role == Role.ADMIN]

    def target(username: str) -> User:
        user = users.get(username.strip().lower())
        if user is None:
            raise HTTPException(404, f"no user {username!r}")
        return user

    @app.get("/admin/users")
    def admin_users(account: Annotated[Account, Depends(get_admin)]) -> list[dict]:
        tenants = list_tenants(settings.corpus_dir, count_documents=False)
        return [user_view(u, tenants) for u in users.list()]

    @app.post("/admin/users")
    def add_user(body: UserCreate, account: Annotated[Account, Depends(get_admin)]) -> dict:
        role = _role(body.role)
        tenants = check_tenant_names(body.tenants)
        try:
            user = users.create(
                body.username, body.password, role, tenants, must_change=body.must_change
            )
        except PasswordPolicyError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, f"user {body.username!r} already exists") from exc
        log.info("%s created user %s (%s)", account.name, user.username, role.label)
        return user_view(user, list_tenants(settings.corpus_dir, count_documents=False))

    @app.patch("/admin/users/{username}")
    def update_user(
        username: str, body: UserUpdate, account: Annotated[Account, Depends(get_admin)]
    ) -> dict:
        user = target(username)
        if body.tenants is not None:
            user = users.set_tenants(user.username, check_tenant_names(body.tenants))
        if body.role is not None:
            role = _role(body.role)
            if role != user.role and user.role == Role.ADMIN:
                if user.username == account.name:
                    raise HTTPException(400, "you can't remove your own admin role")
                if admins() == [user.username]:
                    raise HTTPException(
                        400, "this is the last admin; make someone else admin first"
                    )
            if role != user.role:
                user = users.set_role(user.username, role)
        return user_view(user, list_tenants(settings.corpus_dir, count_documents=False))

    @app.post("/admin/users/{username}/password")
    def reset_password(
        username: str, body: PasswordReset, account: Annotated[Account, Depends(get_admin)]
    ) -> dict:
        user = target(username)
        try:
            check_password_policy(body.password, user.username)
        except PasswordPolicyError as exc:
            raise HTTPException(422, str(exc)) from exc
        users.set_password(user.username, body.password, must_change=True)
        return {"username": user.username, "must_change_password": True}

    @app.delete("/admin/users/{username}")
    def delete_user(username: str, account: Annotated[Account, Depends(get_admin)]) -> dict:
        user = target(username)
        if user.username == account.name:
            raise HTTPException(400, "you can't delete your own account")
        if user.role == Role.ADMIN and admins() == [user.username]:
            raise HTTPException(400, "this is the last admin")
        users.delete(user.username)
        log.info("%s deleted user %s", account.name, user.username)
        return {"deleted": user.username}

    return jobs
