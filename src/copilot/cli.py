"""Command line: ``copilot ingest | ask | token | eval | serve``."""

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


def _cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    uvicorn.run("copilot.api:app_factory", factory=True, host=args.host, port=args.port)


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
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
