"""Command line: ``copilot ingest | add-doc | tenants | ask | token | eval | users | serve``."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from copilot.config import get_settings
from copilot.models import Principal, Role


def _cmd_ingest(args: argparse.Namespace) -> None:
    from copilot.service import ingest

    print(json.dumps(ingest(get_settings()), indent=2))


def _choose(prompt: str, options: list[str]) -> str:
    """Ask on the terminal until one of ``options`` (or its number) is given."""
    if not sys.stdin.isatty():
        sys.exit(f"missing {prompt.lower()}; pass it as an option ({', '.join(options)})")
    print(f"{prompt}:")
    for i, opt in enumerate(options, start=1):
        print(f"  {i}. {opt}")
    while True:
        answer = input("> ").strip().lower()
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1]
        if answer in options:
            return answer
        print(f"Please type a number from 1 to {len(options)}.")


def _cmd_add_doc(args: argparse.Namespace) -> None:
    from copilot.ingest.convert import import_document

    corpus = Path(args.corpus_dir) if args.corpus_dir else get_settings().corpus_dir
    tenants = sorted(p.name for p in corpus.iterdir() if p.is_dir()) if corpus.is_dir() else []
    tenant = args.tenant or _choose("Tenant", tenants or ["policy", "engineering"])
    role = args.min_role or _choose("Minimum role", [r.label for r in Role])
    if tenants and tenant not in tenants:
        sys.exit(f"error: no tenant {tenant!r}; create it first with 'tenants add {tenant}'")
    try:
        out = import_document(
            Path(args.file),
            corpus,
            tenant,
            role,
            title=args.title,
            doc_id=args.doc_id,
            source_label=args.source,
            overwrite=args.force,
        )
    except (ValueError, FileExistsError, FileNotFoundError, RuntimeError) as exc:
        sys.exit(f"error: {exc}")
    lines = out.read_text(encoding="utf-8").splitlines()
    sections = sum(1 for line in lines if line.startswith(("## ", "### ", "#### ")))
    print(f"wrote {out} ({role}, tenant {tenant}, {sections} sections)")
    if not args.no_suggest:
        _suggest_for(out, corpus / tenant, tenant)
    print("Review it, then re-index so the copilot can use it.")


def _suggest_for(doc_path: Path, tenant_dir: Path, tenant: str) -> None:
    """Write likely questions about the new document into the tenant's tenant.yaml."""
    from copilot.ingest.loaders import load_markdown
    from copilot.service import make_llm
    from copilot.suggestions import add_to_tenant, document_questions

    settings = get_settings()
    llm = None if settings.llm == "echo" else make_llm(settings)
    if llm is not None:
        print("writing suggested questions with the LLM...")
    by_role, used_llm = document_questions(load_markdown(doc_path, tenant), llm)
    if not by_role:
        return
    if not add_to_tenant(tenant_dir, by_role):
        print("  ! tenant.yaml has an error, so no suggested questions were added")
        return
    how = "written by the LLM" if used_llm else "made from the section headings"
    print(f"added suggested questions to {tenant_dir / 'tenant.yaml'} ({how}):")
    for r, questions in by_role.items():
        for q in questions:
            print(f"  {r:<9} {q}")


def _cmd_ask(args: argparse.Namespace) -> None:
    from copilot.service import CopilotService

    service = CopilotService.from_settings(get_settings())
    p = Principal(user_id="cli", role=Role.parse(args.role), tenant=args.tenant)
    ans = service.answer(p, args.question, mode=args.mode, use_cache=not args.no_cache)
    print(ans.text)
    if ans.citations:
        print()
        for c in ans.citations:
            print(f"[{c.marker}] {c.title} — {c.section}  ({c.chunk_id})")
    if ans.uncited_sentences:
        print("\nUncited sentences:", *ans.uncited_sentences, sep="\n  - ")
    print(
        f"\n{json.dumps({k: round(v, 1) for k, v in ans.timings_ms.items()})}"
        + (" (cached)" if ans.cached else "")
    )


def _cmd_token(args: argparse.Namespace) -> None:
    from copilot.auth import issue_token

    print(issue_token(get_settings(), args.user, args.role, args.tenant))


def _users():
    from copilot.users import UserStore

    s = get_settings()
    return UserStore(s.users_db, s.password_hash_iterations)


def _read_password(args: argparse.Namespace) -> str:
    import getpass

    if args.password:
        return args.password
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Repeat password: "):
        sys.exit("passwords do not match")
    return pw


def _cmd_users_add(args: argparse.Namespace) -> None:
    user = _users().create(
        args.username,
        _read_password(args),
        args.role,
        args.tenants.split(","),
        must_change=not args.no_force_change,
    )
    print(f"created {user.username} ({user.role.label}, tenants: {', '.join(user.tenants)})")


def _cmd_users_list(args: argparse.Namespace) -> None:
    from copilot.tenants import list_tenants

    tenants = list_tenants(get_settings().corpus_dir)
    for u in _users().list():
        flag = "  [must change password]" if u.must_change else ""
        via_role = [t.name for t in tenants if u.role.label in t.roles and t.name not in u.tenants]
        extra = f" (+{','.join(via_role)} by role)" if via_role else ""
        print(f"{u.username:<20} {u.role.label:<9} {','.join(u.tenants)}{extra}{flag}")


def _cmd_users_reset(args: argparse.Namespace) -> None:
    _users().set_password(args.username, _read_password(args), must_change=True)
    print(f"password reset for {args.username}; they must change it at next login")


def _cmd_users_grant(args: argparse.Namespace) -> None:
    from copilot.tenants import list_tenants

    known = {t.name for t in list_tenants(_corpus(args))}
    unknown = [t for t in args.tenants if t not in known]
    if unknown:
        sys.exit(f"error: no such tenant: {', '.join(unknown)} (create it with 'tenants add')")
    store = _users()
    user = store.get(args.username)
    if user is None:
        sys.exit(f"error: no such user: {args.username}")
    user = store.set_tenants(user.username, user.tenants + args.tenants)
    print(f"{user.username} now has: {', '.join(user.tenants)}")


def _cmd_users_revoke(args: argparse.Namespace) -> None:
    store = _users()
    user = store.get(args.username)
    if user is None:
        sys.exit(f"error: no such user: {args.username}")
    try:
        user = store.set_tenants(user.username, [t for t in user.tenants if t not in args.tenants])
    except ValueError:
        sys.exit("error: a user needs at least one tenant; grant another before revoking this one")
    print(f"{user.username} now has: {', '.join(user.tenants)}")
    from copilot.tenants import list_tenants

    for t in list_tenants(get_settings().corpus_dir):
        if t.name in args.tenants and user.role.label in t.roles:
            print(f"  note: {t.name} is open to every {user.role.label}, so they can still use it")


def _corpus(args: argparse.Namespace) -> Path:
    return Path(args.corpus_dir) if getattr(args, "corpus_dir", None) else get_settings().corpus_dir


def _cmd_tenants_list(args: argparse.Namespace) -> None:
    from copilot.tenants import list_tenants

    users = _users().list()
    for t in list_tenants(_corpus(args)):
        names = [u.username for u in users if t.name in u.tenants or u.role.label in t.roles]
        roles = f"   roles: {', '.join(t.roles)}" if t.roles else ""
        print(
            f"{t.name:<16} {t.label:<24} {t.documents:>3} docs"
            f"   users: {', '.join(names) or '-'}{roles}"
        )


def _cmd_tenants_add(args: argparse.Namespace) -> None:
    from copilot.tenants import create_tenant, split_grant

    roles, names = split_grant((args.grant or "").split(","))
    try:
        tenant = create_tenant(_corpus(args), args.name, args.label, roles, exist_ok=args.exist_ok)
    except (ValueError, FileExistsError) as exc:
        sys.exit(f"error: {exc}")
    print(f"tenant {tenant.name} ({tenant.label}) is ready")
    if tenant.roles:
        print(f"  open to every {' and '.join(tenant.roles)}, including users added later")
    store = _users()
    if args.grant_all:
        names = [u.username for u in store.list()]
    for name in names:
        user = store.get(name)
        if user is None:
            print(f"  ! no such user or role: {name}")
            continue
        store.set_tenants(user.username, user.tenants + [tenant.name])
        print(f"  gave {user.username} access")
    print("Next: add documents with 'add-doc --tenant " + tenant.name + "', then run ingest.")


def _cmd_eval(args: argparse.Namespace) -> None:
    from copilot.eval.runner import evaluate, format_table, load_questions
    from copilot.retrieval.vector import load_chunks
    from copilot.service import CopilotService

    s = get_settings()
    service = CopilotService.from_settings(s)
    questions = load_questions(Path(args.questions))
    chunks = load_chunks(s.index_dir / "chunks.jsonl")
    report = evaluate(service, questions, chunks, args.modes.split(","), args.retrieval_only)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(format_table(report))
    print(f"\nFull report: {out}")
    total_leaks = sum(m["leaks"] for m in report["modes"].values())
    total_leaks += report.get("cache_poisoning_leaks", 0)
    if total_leaks:
        print(f"FAIL: {total_leaks} leak(s)", file=sys.stderr)
        sys.exit(1)


def _lan_addresses() -> list[str]:
    """Best-effort list of this machine's IPv4 addresses (no traffic is sent)."""
    import socket

    addrs: set[str] = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # TEST-NET address; connect() on UDP sends nothing
            addrs.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addrs.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addrs if not a.startswith("127."))


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    s = get_settings()
    host = args.host or s.host
    port = args.port or s.port
    if host == "0.0.0.0":
        urls = [f"http://localhost:{port}"] + [f"http://{a}:{port}" for a in _lan_addresses()]
    else:
        urls = [f"http://{host}:{port}"]
    print("Knowledge Copilot web UI:", *urls, sep="\n  ")
    uvicorn.run("copilot.api:app_factory", factory=True, host=host, port=port)


def main(argv: list[str] | None = None) -> None:
    # Windows consoles default to a legacy code page; don't crash on characters it lacks.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="copilot")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("ingest", help="build the index from the corpus directory")
    p.set_defaults(func=_cmd_ingest)

    p = sub.add_parser("add-doc", help="convert a PDF or DOCX into a tagged Markdown document")
    p.add_argument("file", help="the .pdf or .docx to import")
    p.add_argument("--tenant", help="corpus folder, e.g. policy or engineering (asked if omitted)")
    p.add_argument(
        "--min-role", choices=[r.label for r in Role], help="lowest role that may see it"
    )
    p.add_argument("--title", help="default: the file's own title or its name")
    p.add_argument("--doc-id", help="default: the file name")
    p.add_argument("--source", help="where it came from (default: the file name)")
    p.add_argument("--corpus-dir", help="default: COPILOT_CORPUS_DIR or data/corpus")
    p.add_argument("--force", action="store_true", help="replace an existing document")
    p.add_argument(
        "--no-suggest", action="store_true", help="don't add suggested questions to tenant.yaml"
    )
    p.set_defaults(func=_cmd_add_doc)

    p = sub.add_parser("ask", help="ask a question as a given role")
    p.add_argument("question")
    p.add_argument("--role", default="employee", choices=[r.label for r in Role])
    p.add_argument("--tenant", default="policy")
    p.add_argument("--mode", default=None)
    p.add_argument("--no-cache", action="store_true")
    p.set_defaults(func=_cmd_ask)

    p = sub.add_parser("token", help="issue a JWT for local testing")
    p.add_argument("--user", default="demo")
    p.add_argument("--role", default="employee", choices=[r.label for r in Role])
    p.add_argument("--tenant", default="policy")
    p.set_defaults(func=_cmd_token)

    p = sub.add_parser("eval", help="run the evaluation set")
    p.add_argument("--questions", default="eval/questions.jsonl")
    p.add_argument("--modes", default="bm25,vector,hybrid,hybrid_rerank")
    p.add_argument("--retrieval-only", action="store_true", help="skip the LLM")
    p.add_argument("--out", default="reports/eval.json")
    p.set_defaults(func=_cmd_eval)

    users = sub.add_parser("users", help="manage web UI accounts").add_subparsers(required=True)
    p = users.add_parser("add", help="create a user")
    p.add_argument("username")
    p.add_argument("--role", required=True, choices=[r.label for r in Role])
    p.add_argument("--tenants", default="policy,engineering", help="comma-separated")
    p.add_argument("--password", help="omit to be prompted")
    p.add_argument("--no-force-change", action="store_true", help="skip first-login change")
    p.set_defaults(func=_cmd_users_add)
    p = users.add_parser("list", help="list users")
    p.set_defaults(func=_cmd_users_list)
    p = users.add_parser("reset-password", help="set a new password (must change at login)")
    p.add_argument("username")
    p.add_argument("--password", help="omit to be prompted")
    p.set_defaults(func=_cmd_users_reset)

    p = users.add_parser("grant", help="give a user access to tenants")
    p.add_argument("username")
    p.add_argument("tenants", nargs="+")
    p.add_argument("--corpus-dir", help="default: COPILOT_CORPUS_DIR or data/corpus")
    p.set_defaults(func=_cmd_users_grant)
    p = users.add_parser("revoke", help="remove a user's access to tenants")
    p.add_argument("username")
    p.add_argument("tenants", nargs="+")
    p.set_defaults(func=_cmd_users_revoke)

    tenants = sub.add_parser("tenants", help="manage tenants (corpus folders)")
    tenants = tenants.add_subparsers(required=True)
    p = tenants.add_parser("list", help="list tenants, document counts and users")
    p.add_argument("--corpus-dir", help="default: COPILOT_CORPUS_DIR or data/corpus")
    p.set_defaults(func=_cmd_tenants_list)
    p = tenants.add_parser("add", help="create a tenant and optionally give users access")
    p.add_argument("name", help="folder name, e.g. legal")
    p.add_argument("--label", help="name shown in the web UI, e.g. 'Legal'")
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--grant",
        help="comma-separated roles and/or users, e.g. admin,manager or alice,bob."
        " A role covers users added later too",
    )
    g.add_argument("--grant-all", action="store_true", help="give every existing user access")
    p.add_argument("--corpus-dir", help="default: COPILOT_CORPUS_DIR or data/corpus")
    p.add_argument("--exist-ok", action="store_true", help="don't fail if it exists; just grant")
    p.set_defaults(func=_cmd_tenants_add)

    p = sub.add_parser("serve", help="run the HTTP API and web UI")
    p.add_argument("--host", help="address to listen on (default: COPILOT_HOST or 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default: COPILOT_PORT or 8000)")
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
