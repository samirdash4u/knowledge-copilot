#!/usr/bin/env python3
"""Cross-platform helper for running Knowledge Copilot with Docker Compose.

Works the same on Windows (PowerShell or cmd), Linux and macOS, and needs only Python 3.9+ and
Docker. It replaces the Makefile targets, which need ``make`` and a POSIX shell.

    python scripts/manage.py setup        create .env with a random JWT secret
    python scripts/manage.py up           build and start everything
    python scripts/manage.py pull-model   download the LLM into Ollama
    python scripts/manage.py add-doc F    convert a PDF/DOCX into a tagged document in data/corpus
    python scripts/manage.py add-tenant N create a tenant (corpus folder) and give users access
    python scripts/manage.py users ...    manage accounts: list, add, grant, revoke, reset-password
    python scripts/manage.py ingest       index the documents
    python scripts/manage.py status       health check and the URLs to open
    python scripts/manage.py logs         follow the API logs
    python scripts/manage.py eval         run the evaluation set
    python scripts/manage.py down         stop everything (data is kept)
    python scripts/manage.py clean        remove containers and generated files (see --help)

Set COPILOT_GPU=true in .env (or pass --gpu to ``up``) to run the LLM on an NVIDIA GPU.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
DEFAULT_MODEL = "qwen2.5:7b-instruct-q4_K_M"
PLACEHOLDER_SECRET = "replace-with-a-long-random-string-of-at-least-32-bytes"


def fail(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def setup(_: argparse.Namespace | None = None) -> None:
    if ENV_FILE.exists():
        print(".env already exists, leaving it unchanged")
    else:
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        text = text.replace(PLACEHOLDER_SECRET, secrets.token_urlsafe(48))
        ENV_FILE.write_text(text, encoding="utf-8")
        print("created .env with a random COPILOT_JWT_SECRET")
    env = read_env()
    if env.get("COPILOT_JWT_SECRET", PLACEHOLDER_SECRET) == PLACEHOLDER_SECRET:
        print("warning: COPILOT_JWT_SECRET in .env is still the placeholder; set a random value")


def docker_check() -> None:
    if shutil.which("docker") is None:
        fail(
            "docker was not found on PATH. Install Docker Desktop (Windows/macOS) or Docker Engine"
            " with the compose plugin (Linux), then open a new terminal."
        )
    probe = subprocess.run(["docker", "info"], capture_output=True, text=True)
    if probe.returncode != 0:
        fail(
            "the Docker daemon is not reachable. On Windows start Docker Desktop and wait until it"
            " says 'Engine running'. On Linux run 'sudo systemctl start docker' and make sure your"
            " user is in the 'docker' group (log out and back in after adding it)."
        )
    compose = subprocess.run(["docker", "compose", "version"], capture_output=True, text=True)
    if compose.returncode != 0:
        fail("'docker compose' is not available. Install the Docker Compose v2 plugin.")


def compose_cmd(gpu: bool | None = None) -> list[str]:
    if gpu is None:
        gpu = read_env().get("COPILOT_GPU", "false").lower() in {"1", "true", "yes"}
    cmd = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]
    if gpu:
        cmd += ["-f", str(ROOT / "docker-compose.gpu.yml")]
    return cmd


def run(cmd: list[str]) -> None:
    print("+ " + " ".join(cmd))
    result = subprocess.run(cmd, cwd=ROOT)
    if result.returncode != 0:
        sys.exit(result.returncode)


def lan_addresses() -> list[str]:
    """Best-effort list of this machine's IPv4 addresses (no traffic is sent)."""
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


def urls() -> list[str]:
    env = read_env()
    host = env.get("COPILOT_HOST", "0.0.0.0")
    port = env.get("COPILOT_PORT", "8000")
    if host in {"0.0.0.0", ""}:
        return [f"http://localhost:{port}"] + [f"http://{a}:{port}" for a in lan_addresses()]
    return [f"http://{host}:{port}"]


def wait_healthy(timeout_s: int = 120) -> bool:
    port = read_env().get("COPILOT_PORT", "8000")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(2)
    return False


def up(args: argparse.Namespace) -> None:
    setup()
    docker_check()
    run(compose_cmd(True if args.gpu else None) + ["up", "-d", "--build"])
    print("waiting for the API to become healthy...")
    if wait_healthy():
        status(args)
        print(
            "\nNext: 'python scripts/manage.py pull-model' then 'python scripts/manage.py ingest'"
        )
    else:
        print("the API did not answer yet; check 'python scripts/manage.py logs'")


def down(_: argparse.Namespace) -> None:
    docker_check()
    run(compose_cmd() + ["down"])


def pull_model(_: argparse.Namespace) -> None:
    docker_check()
    model = read_env().get("COPILOT_OLLAMA_MODEL", DEFAULT_MODEL)
    run(compose_cmd() + ["exec", "ollama", "ollama", "pull", model])


ROLES = ["employee", "manager", "admin"]


def choose(prompt: str, options: list[str]) -> str:
    """Ask on the terminal until one of ``options`` (or its number) is given."""
    if not sys.stdin.isatty():
        fail(f"missing {prompt.lower()}; pass it as an option ({', '.join(options)})")
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


def add_doc(args: argparse.Namespace) -> None:
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        fail(f"no such file: {args.file}")
    if source.suffix.lower() not in {".pdf", ".docx"}:
        fail(f"{source.name} is not a .pdf or .docx file")
    corpus = ROOT / "data" / "corpus"
    tenants = sorted(p.name for p in corpus.iterdir() if p.is_dir())
    tenant = args.tenant or choose("Tenant", tenants)
    role = args.min_role or choose("Minimum role (who may see it)", ROLES)
    docker_check()
    cmd = corpus_run(corpus) + [
        "--volume", f"{source.parent}:/input:ro",
        "api", "copilot", "add-doc", f"/input/{source.name}",
        "--corpus-dir", "/corpus", "--tenant", tenant, "--min-role", role,
    ]  # fmt: skip
    for flag in ("title", "doc_id", "source"):
        value = getattr(args, flag)
        if value:
            cmd += [f"--{flag.replace('_', '-')}", value]
    if args.force:
        cmd.append("--force")
    if args.no_suggest:
        cmd.append("--no-suggest")
    run(cmd)
    if args.ingest:
        ingest(args)
    else:
        print("Next: 'python scripts/manage.py ingest' to index it")


def corpus_run(corpus: Path) -> list[str]:
    """``docker compose run`` for a command that writes to data/corpus.

    The corpus is read-only inside the API container, so mount it writable at /corpus for this
    one run. --build picks up new dependencies.
    """
    cmd = compose_cmd() + ["run", "--rm", "--build", "--no-deps"]
    if hasattr(os, "getuid"):  # Linux/macOS: create files as you, not as root
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    return cmd + ["--volume", f"{corpus}:/corpus"]


TENANT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


def add_tenant(args: argparse.Namespace) -> None:
    if not TENANT_NAME.fullmatch(args.name):
        fail(f"tenant name must be lowercase letters, digits, '-' or '_', got {args.name!r}")
    folder = ROOT / "data" / "corpus" / args.name
    if folder.exists():
        fail(f"tenant {args.name!r} already exists ({folder})")
    items = [i.strip().lower() for i in (args.grant or "").split(",") if i.strip()]
    roles = list(dict.fromkeys(i for i in items if i in ROLES))
    names = [i for i in items if i not in ROLES]
    label = (args.label or "").strip() or args.name.replace("-", " ").replace("_", " ").title()
    folder.mkdir(parents=True)
    # JSON strings and lists are valid YAML, so no YAML library is needed here.
    meta = f"label: {json.dumps(label)}\n"
    if roles:
        meta += f"roles: {json.dumps(roles)}\n"
    (folder / "tenant.yaml").write_text(meta, encoding="utf-8")
    print(f"created tenant {args.name} ({label}) in {folder}")
    if roles:
        print(f"  open to every {' and '.join(roles)}, including users added later")
    if names or args.grant_all:
        # User accounts live in the API's data volume, so the grant runs in the container.
        docker_check()
        cmd = compose_cmd() + ["run", "--rm", "--build", "--no-deps", "api"]
        cmd += ["copilot", "tenants", "add", args.name, "--exist-ok"]
        cmd += ["--grant-all"] if args.grant_all else ["--grant", ",".join(names)]
        run(cmd)
    elif not roles:
        print(f"Give users access with: python scripts/manage.py users grant <user> {args.name}")
    print(f"Next: python scripts/manage.py add-doc <file> --tenant {args.name}, then ingest")


def users(args: argparse.Namespace) -> None:
    docker_check()
    run(
        compose_cmd()
        + ["run", "--rm", "--build", "--no-deps", "api", "copilot", "users", *args.rest]
    )


def ingest(_: argparse.Namespace) -> None:
    docker_check()
    run(compose_cmd() + ["run", "--rm", "--build", "api", "copilot", "ingest"])
    # The API loads the index once, so start a fresh API container. 'restart' would keep the old
    # container and with it the old image, so code pulled since the last 'up' would not run.
    run(compose_cmd() + ["up", "-d", "--no-deps", "--force-recreate", "api"])
    if not wait_healthy():
        print("the API did not answer yet; check 'python scripts/manage.py logs'")


def evaluate(_: argparse.Namespace) -> None:
    docker_check()
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    run(compose_cmd() + ["run", "--rm", "-v", f"{reports}:/app/reports", "api", "copilot", "eval"])


def logs(_: argparse.Namespace) -> None:
    docker_check()
    run(compose_cmd() + ["logs", "-f", "api"])


def status(_: argparse.Namespace) -> None:
    port = read_env().get("COPILOT_PORT", "8000")
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
            ok = r.status == 200
    except (urllib.error.URLError, OSError):
        ok = False
    print(f"API health: {'ok' if ok else 'not reachable'}")
    print("Open the web UI at:")
    for u in urls():
        print(f"  {u}")


# Docker volumes by what they hold. Names match docker-compose.yml.
DATA_VOLUMES = ["index", "qdrant"]  # vector index, chunk store and the users database
MODEL_VOLUMES = ["ollama", "hf-models"]  # downloaded LLM and embedding/reranker models

# Files created by ingest, eval, tests and tooling; always safe to delete.
GENERATED_PATHS = [
    "data/index",
    "reports/eval.json",
    "reports/eval-offline.json",
    ".pytest_cache",
    ".ruff_cache",
]
USER_DATA_PATHS = ["data/users.db", "data/users.db-journal"]


def project_name() -> str:
    """Compose project name, which prefixes the volume names."""
    try:
        out = subprocess.run(
            compose_cmd() + ["config", "--format", "json"],
            capture_output=True,
            text=True,
            cwd=ROOT,
        )
        if out.returncode == 0:
            return json.loads(out.stdout)["name"]
    except (OSError, ValueError, KeyError):
        pass
    return "".join(c for c in ROOT.name.lower() if c.isalnum() or c in "-_")


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


def _force_remove(func, path, _exc) -> None:
    # Windows marks some files (e.g. inside .venv or .git) read-only; clear the flag and retry.
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=_force_remove)
        else:
            shutil.rmtree(path, onerror=_force_remove)
    elif path.exists() or path.is_symlink():
        path.unlink()


def clean(args: argparse.Namespace) -> None:
    if args.all:
        args.data = args.models = args.images = True

    volumes: list[str] = []
    if args.data:
        volumes += DATA_VOLUMES
    if args.models:
        volumes += MODEL_VOLUMES

    local = [ROOT / p for p in GENERATED_PATHS]
    local += sorted((ROOT / "src").rglob("__pycache__")) + sorted(
        (ROOT / "tests").rglob("__pycache__")
    )
    if args.data:
        local += [ROOT / p for p in USER_DATA_PATHS]
    if args.all:
        local += [ROOT / ".venv", ENV_FILE]
    local = [p for p in local if p.exists()]

    use_docker = docker_available()
    project = project_name() if use_docker else ""

    print("This will:")
    if use_docker:
        print("  - stop and remove the Knowledge Copilot containers and network")
        if args.images:
            print("  - remove the locally built API image")
        for v in volumes:
            print(f"  - delete Docker volume {project}_{v}")
    else:
        print("  - (Docker is not running, so containers and volumes are left alone)")
    for p in local:
        print(f"  - delete {p.relative_to(ROOT)}")
    if args.data:
        print(
            "  ! user accounts and passwords will be lost; demo users are recreated on next start"
        )
    if args.models:
        print("  ! models will be downloaded again (about 6 GB) on the next pull-model / ingest")
    if args.all:
        print("  ! .env (your settings and JWT secret) will be deleted")

    if args.dry_run:
        print("\n--dry-run: nothing was changed")
        return
    if not args.yes:
        answer = input("\nContinue? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("cancelled")
            return

    if use_docker:
        down_cmd = compose_cmd() + ["down", "--remove-orphans"]
        if args.images:
            down_cmd += ["--rmi", "local"]
        run(down_cmd)
        for v in volumes:
            name = f"{project}_{v}"
            exists = (
                subprocess.run(
                    ["docker", "volume", "inspect", name], capture_output=True
                ).returncode
                == 0
            )
            if exists:
                run(["docker", "volume", "rm", name])
    for p in local:
        remove_path(p)
        print(f"deleted {p.relative_to(ROOT)}")
    print("clean-up finished")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(required=True)
    sub.add_parser("setup", help="create .env").set_defaults(func=setup)
    p = sub.add_parser("up", help="build and start all services")
    p.add_argument("--gpu", action="store_true", help="run the LLM on an NVIDIA GPU")
    p.set_defaults(func=up)
    sub.add_parser("down", help="stop all services").set_defaults(func=down)
    sub.add_parser("pull-model", help="download the LLM").set_defaults(func=pull_model)
    p = sub.add_parser("add-doc", help="convert a PDF or DOCX into a document in data/corpus")
    p.add_argument("file", help="the .pdf or .docx to import")
    p.add_argument("--tenant", help="corpus folder, e.g. policy or engineering (asked if omitted)")
    p.add_argument("--min-role", choices=ROLES, help="who may see it (asked if omitted)")
    p.add_argument("--title", help="default: the file's own title or its name")
    p.add_argument("--doc-id", help="default: the file name")
    p.add_argument("--source", help="where it came from (default: the file name)")
    p.add_argument("--force", action="store_true", help="replace an existing document")
    p.add_argument("--ingest", action="store_true", help="re-index straight away")
    p.add_argument(
        "--no-suggest", action="store_true", help="don't add suggested questions to tenant.yaml"
    )
    p.set_defaults(func=add_doc)
    p = sub.add_parser("add-tenant", help="create a tenant and optionally give users access")
    p.add_argument("name", help="folder name, e.g. legal")
    p.add_argument("--label", help="name shown in the web UI, e.g. 'Legal'")
    g = p.add_mutually_exclusive_group()
    g.add_argument(
        "--grant",
        help="comma-separated roles and/or users, e.g. admin,manager or alice,bob."
        " A role covers users added later too",
    )
    g.add_argument("--grant-all", action="store_true", help="give every existing user access")
    p.set_defaults(func=add_tenant)
    p = sub.add_parser(
        "users",
        help="manage web UI accounts (list, add, grant, revoke, reset-password)",
        add_help=False,
    )
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(func=users)
    sub.add_parser("ingest", help="index the documents").set_defaults(func=ingest)
    sub.add_parser("eval", help="run the evaluation set").set_defaults(func=evaluate)
    sub.add_parser("logs", help="follow API logs").set_defaults(func=logs)
    sub.add_parser("status", help="health check and URLs").set_defaults(func=status)
    p = sub.add_parser(
        "clean",
        help="remove containers and generated files; flags remove more",
        description=(
            "Default: stop and remove containers, delete the generated index, reports and caches."
            " Documents in data/corpus are never touched."
        ),
    )
    p.add_argument(
        "--data",
        action="store_true",
        help="also delete the index, Qdrant and user-account volumes and data/users.db",
    )
    p.add_argument(
        "--models",
        action="store_true",
        help="also delete downloaded LLM and embedding models (about 6 GB)",
    )
    p.add_argument("--images", action="store_true", help="also remove the built API image")
    p.add_argument(
        "--all",
        action="store_true",
        help="everything above, plus .env and .venv: back to a fresh checkout",
    )
    p.add_argument("--dry-run", action="store_true", help="show what would be removed")
    p.add_argument("-y", "--yes", action="store_true", help="don't ask for confirmation")
    p.set_defaults(func=clean)
    # 'users' passes everything after it to 'copilot users', including options like --help.
    args, extra = parser.parse_known_args()
    if args.func is users:
        args.rest = extra + args.rest
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    args.func(args)


if __name__ == "__main__":
    main()
