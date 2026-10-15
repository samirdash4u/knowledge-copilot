"""Tenants are the top-level folders of the corpus directory.

A tenant can carry settings in ``<corpus>/<tenant>/tenant.yaml``::

    label: Legal              # shown in the web UI's workspace picker
    roles: [admin, manager]   # every user with one of these roles may use the tenant

Documents are isolated per tenant. A user may use the tenants listed on their account plus any
tenant whose ``roles`` include their role, so a manager created later gets access too.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

TENANT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TENANT_FILE = "tenant.yaml"
BUILTIN_LABELS = {"policy": "Company policy", "engineering": "Engineering docs"}


ROLE_NAMES = ("employee", "manager", "admin")


@dataclass
class Tenant:
    name: str
    label: str
    documents: int
    roles: list[str] = field(default_factory=list)


def check_name(name: str) -> str:
    if not TENANT_NAME.fullmatch(name):
        raise ValueError(
            f"tenant name must be lowercase letters, digits, '-' or '_' (up to 40), got {name!r}"
        )
    return name


def read_meta(folder: Path) -> dict:
    """The raw contents of ``tenant.yaml``, or {} when it is missing or not a mapping."""
    meta_file = folder / TENANT_FILE
    if not meta_file.is_file():
        return {}
    try:
        meta = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        # A typo in one tenant's file must not break sign-in for every tenant.
        log.warning("ignoring %s: %s", meta_file, exc)
        return {}
    return meta if isinstance(meta, dict) else {}


def _read_meta(folder: Path) -> tuple[str, list[str]]:
    label = BUILTIN_LABELS.get(folder.name, folder.name.replace("-", " ").replace("_", " ").title())
    meta = read_meta(folder)
    label = str(meta.get("label") or label)
    raw = meta.get("roles") or []
    roles = [r for r in ([raw] if isinstance(raw, str) else raw) if r in ROLE_NAMES]
    return label, roles


def list_tenants(corpus_dir: Path, count_documents: bool = True) -> list[Tenant]:
    from copilot.ingest.loaders import SUPPORTED

    if not corpus_dir.is_dir():
        return []
    tenants = []
    builtin = list(BUILTIN_LABELS)
    folders = [p for p in corpus_dir.iterdir() if p.is_dir()]
    # The original two first (policy is the default workspace), then the rest alphabetically.
    folders.sort(
        key=lambda p: (builtin.index(p.name) if p.name in builtin else len(builtin), p.name)
    )
    for folder in folders:
        if not TENANT_NAME.fullmatch(folder.name):
            continue
        docs = (
            sum(1 for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED)
            if count_documents
            else 0
        )
        label, roles = _read_meta(folder)
        tenants.append(Tenant(folder.name, label, docs, roles))
    return tenants


def check_roles(roles: list[str]) -> list[str]:
    bad = [r for r in roles if r not in ROLE_NAMES]
    if bad:
        raise ValueError(f"unknown role: {', '.join(bad)} (use {', '.join(ROLE_NAMES)})")
    return list(dict.fromkeys(roles))


def write_meta(folder: Path, label: str, roles: list[str]) -> None:
    """Set the label and roles in ``tenant.yaml``, keeping any other keys (such as suggestions)."""
    meta: dict = {**read_meta(folder), "label": label}
    if roles:
        meta["roles"] = roles
    else:
        meta.pop("roles", None)
    (folder / TENANT_FILE).write_text(
        yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=None),
        encoding="utf-8",
    )


def create_tenant(
    corpus_dir: Path,
    name: str,
    label: str | None = None,
    roles: list[str] | None = None,
    exist_ok: bool = False,
) -> Tenant:
    """Create ``<corpus>/<name>/`` with its ``tenant.yaml``.

    An existing tenant is an error, unless ``exist_ok``: then the given ``roles`` are added to it.
    """
    folder = corpus_dir / check_name(name)
    roles = check_roles(roles or [])
    if folder.exists():
        if not (exist_ok and folder.is_dir()):
            raise FileExistsError(f"tenant {name!r} already exists ({folder})")
        tenant = next(t for t in list_tenants(corpus_dir) if t.name == name)
        if roles:
            tenant.roles = check_roles(tenant.roles + roles)
            write_meta(folder, tenant.label, tenant.roles)
        return tenant
    folder.mkdir(parents=True)
    label = (label or "").strip() or name.replace("-", " ").replace("_", " ").title()
    write_meta(folder, label, roles)
    return Tenant(name, label, 0, roles)


def tenants_for(role: str, own: list[str], tenants: list[Tenant]) -> list[str]:
    """A user's tenants: those on their account, then those open to their role."""
    by_role = [t.name for t in tenants if role in t.roles]
    return list(dict.fromkeys(own + by_role))


def split_grant(items: list[str]) -> tuple[list[str], list[str]]:
    """Split a ``--grant`` list into (roles, usernames). A role name always means the role."""
    items = [i.strip().lower() for i in items if i.strip()]
    return [i for i in items if i in ROLE_NAMES], [i for i in items if i not in ROLE_NAMES]
