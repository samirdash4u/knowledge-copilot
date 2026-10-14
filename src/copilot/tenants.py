"""Tenants are the top-level folders of the corpus directory.

A tenant can carry a display name in ``<corpus>/<tenant>/tenant.yaml`` (``label: Legal``); the
web UI shows it in the workspace picker. Documents are isolated per tenant, and a user sees only
the tenants listed on their account.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

TENANT_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")
TENANT_FILE = "tenant.yaml"
BUILTIN_LABELS = {"policy": "Company policy", "engineering": "Engineering docs"}


@dataclass
class Tenant:
    name: str
    label: str
    documents: int


def check_name(name: str) -> str:
    if not TENANT_NAME.fullmatch(name):
        raise ValueError(
            f"tenant name must be lowercase letters, digits, '-' or '_' (up to 40), got {name!r}"
        )
    return name


def _label(folder: Path) -> str:
    meta_file = folder / TENANT_FILE
    if meta_file.is_file():
        meta = yaml.safe_load(meta_file.read_text(encoding="utf-8")) or {}
        if isinstance(meta, dict) and meta.get("label"):
            return str(meta["label"])
    return BUILTIN_LABELS.get(folder.name, folder.name.replace("-", " ").replace("_", " ").title())


def list_tenants(corpus_dir: Path) -> list[Tenant]:
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
        docs = sum(1 for f in folder.rglob("*") if f.is_file() and f.suffix.lower() in SUPPORTED)
        tenants.append(Tenant(folder.name, _label(folder), docs))
    return tenants


def create_tenant(
    corpus_dir: Path, name: str, label: str | None = None, exist_ok: bool = False
) -> Tenant:
    """Create ``<corpus>/<name>/`` with its ``tenant.yaml``.

    An existing tenant is an error, unless ``exist_ok``: then it is returned unchanged.
    """
    folder = corpus_dir / check_name(name)
    if folder.exists():
        if exist_ok and folder.is_dir():
            return next(t for t in list_tenants(corpus_dir) if t.name == name)
        raise FileExistsError(f"tenant {name!r} already exists ({folder})")
    folder.mkdir(parents=True)
    label = (label or "").strip() or name.replace("-", " ").replace("_", " ").title()
    (folder / TENANT_FILE).write_text(
        yaml.safe_dump({"label": label}, allow_unicode=True), encoding="utf-8"
    )
    return Tenant(name, label, 0)
