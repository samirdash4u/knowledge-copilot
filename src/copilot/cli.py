"""Command line: ``copilot ingest | add-doc | ask | token | eval | users | serve``."""

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
        print(f"note: {tenant!r} is a new tenant; give it to users with 'copilot users add'")
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
    print("Review it, then re-index so the copilot can use it.")


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
    for u in _users().list():
        flag = "  [must change password]" if u.must_change else ""
        print(f"{u.username:<20} {u.role.label:<9} {','.join(u.tenants)}{flag}")


def _cmd_users_reset(args: argparse.Namespace) -> None:
    _users().set_password(args.username, _read_password(args), must_change=True)
    print(f"password reset for {args.username}; they must change it at next login")


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

    p = sub.add_parser("serve", help="run the HTTP API and web UI")
    p.add_argument("--host", help="address to listen on (default: COPILOT_HOST or 127.0.0.1)")
    p.add_argument("--port", type=int, help="port (default: COPILOT_PORT or 8000)")
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
