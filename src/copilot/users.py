"""User accounts stored in SQLite.

Passwords are hashed with PBKDF2-HMAC-SHA256 (standard library, so it works the same on Linux,
macOS and Windows). Each user has a role and the tenants they may sign in to. A
``token_version`` counter is embedded in every issued token; changing the password bumps it,
which invalidates tokens issued before the change.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from copilot.models import Role

PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LENGTH = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username        TEXT PRIMARY KEY,
    password_hash   TEXT NOT NULL,
    role            TEXT NOT NULL,
    tenants         TEXT NOT NULL,
    must_change     INTEGER NOT NULL DEFAULT 0,
    token_version   INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);
"""

# Demo accounts created on first start so the UI can be tried immediately. Every one must change
# its password at first login.
DEMO_USERS = [
    ("alice", Role.EMPLOYEE),
    ("bob", Role.MANAGER),
    ("carol", Role.ADMIN),
]
DEMO_TENANTS = ["policy", "engineering"]


def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


class PasswordPolicyError(ValueError):
    pass


def check_password_policy(password: str, username: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if not any(c.isalpha() for c in password) or not any(c.isdigit() for c in password):
        raise PasswordPolicyError("password must contain both letters and digits")
    if username.lower() in password.lower():
        raise PasswordPolicyError("password must not contain the username")


@dataclass
class User:
    username: str
    role: Role
    tenants: list[str]
    must_change: bool
    token_version: int


class UserStore:
    def __init__(self, path: Path, iterations: int = PBKDF2_ITERATIONS) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.iterations = iterations
        self._lock = threading.Lock()
        # Spend the same time on unknown usernames as on wrong passwords.
        self._dummy_hash = hash_password(secrets.token_hex(8), iterations)
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # Always close the connection: an open handle keeps the file locked on Windows.
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:  # commits on success, rolls back on error
                yield db
        finally:
            db.close()

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            username=row["username"],
            role=Role.parse(row["role"]),
            tenants=[t for t in row["tenants"].split(",") if t],
            must_change=bool(row["must_change"]),
            token_version=row["token_version"],
        )

    def count(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def get(self, username: str) -> User | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return self._row_to_user(row) if row else None

    def list(self) -> list[User]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM users ORDER BY username").fetchall()
        return [self._row_to_user(r) for r in rows]

    def create(
        self,
        username: str,
        password: str,
        role: Role | str,
        tenants: list[str],
        must_change: bool = True,
        enforce_policy: bool = True,
    ) -> User:
        username = username.strip().lower()
        if not username or not username.replace(".", "").replace("_", "").isalnum():
            raise ValueError("username may contain letters, digits, '.' and '_' only")
        if not tenants:
            raise ValueError("user needs at least one tenant")
        if enforce_policy:
            check_password_policy(password, username)
        now = time.time()
        with self._lock, self._connect() as db:
            db.execute(
                "INSERT INTO users (username, password_hash, role, tenants, must_change,"
                " token_version, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                (
                    username,
                    hash_password(password, self.iterations),
                    Role.parse(role).label,
                    ",".join(tenants),
                    int(must_change),
                    now,
                    now,
                ),
            )
        return self.get(username)  # type: ignore[return-value]

    def set_tenants(self, username: str, tenants: list[str]) -> User:
        """Replace the user's tenants. Removing one ends their sessions in it immediately."""
        tenants = list(dict.fromkeys(t for t in tenants if t))
        if not tenants:
            raise ValueError("user needs at least one tenant")
        with self._lock, self._connect() as db:
            cur = db.execute(
                "UPDATE users SET tenants = ?, updated_at = ? WHERE username = ?",
                (",".join(tenants), time.time(), username.strip().lower()),
            )
            if cur.rowcount == 0:
                raise KeyError(username)
        return self.get(username.strip().lower())  # type: ignore[return-value]

    def set_role(self, username: str, role: Role | str) -> User:
        """Change the user's role. Their current sessions end, since tokens carry the role."""
        username = username.strip().lower()
        with self._lock, self._connect() as db:
            cur = db.execute(
                "UPDATE users SET role = ?, token_version = token_version + 1, updated_at = ?"
                " WHERE username = ?",
                (Role.parse(role).label, time.time(), username),
            )
            if cur.rowcount == 0:
                raise KeyError(username)
        return self.get(username)  # type: ignore[return-value]

    def delete(self, username: str) -> None:
        with self._lock, self._connect() as db:
            cur = db.execute("DELETE FROM users WHERE username = ?", (username.strip().lower(),))
            if cur.rowcount == 0:
                raise KeyError(username)

    def authenticate(self, username: str, password: str) -> User | None:
        username = username.strip().lower()
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            verify_password(password, self._dummy_hash)
            return None
        if not verify_password(password, row["password_hash"]):
            return None
        return self._row_to_user(row)

    def change_password(self, username: str, current: str, new: str) -> User:
        if self.authenticate(username, current) is None:
            raise PermissionError("current password is incorrect")
        if current == new:
            raise PasswordPolicyError("new password must differ from the current one")
        check_password_policy(new, username)
        return self.set_password(username, new, must_change=False)

    def set_password(self, username: str, new: str, must_change: bool = False) -> User:
        with self._lock, self._connect() as db:
            cur = db.execute(
                "UPDATE users SET password_hash = ?, must_change = ?,"
                " token_version = token_version + 1, updated_at = ? WHERE username = ?",
                (hash_password(new, self.iterations), int(must_change), time.time(), username),
            )
            if cur.rowcount == 0:
                raise KeyError(username)
        return self.get(username)  # type: ignore[return-value]

    def seed_demo_users(self, password: str) -> list[str]:
        """Create the demo accounts if the store is empty. Returns the usernames created."""
        if self.count():
            return []
        for name, role in DEMO_USERS:
            self.create(name, password, role, DEMO_TENANTS, must_change=True, enforce_policy=False)
        return [n for n, _ in DEMO_USERS]

    def sync_demo_passwords(self, password: str) -> list[str]:
        """Give demo accounts that were never signed into the current demo password.

        The accounts are created only once, so without this a later change to
        COPILOT_DEMO_PASSWORD would never reach them. Accounts whose owner already set their
        own password are left alone. Returns the usernames updated.
        """
        updated = []
        with self._connect() as db:
            rows = db.execute(
                "SELECT username, password_hash FROM users WHERE must_change = 1"
                " AND username IN (?, ?, ?)",
                [n for n, _ in DEMO_USERS],
            ).fetchall()
        for row in rows:
            if not verify_password(password, row["password_hash"]):
                self.set_password(row["username"], password, must_change=True)
                updated.append(row["username"])
        return updated


class LoginThrottle:
    """Lock a username out for a while after repeated failed logins."""

    def __init__(self, max_failures: int = 5, lockout_s: int = 300) -> None:
        self.max_failures = max_failures
        self.lockout_s = lockout_s
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def locked(self, username: str) -> bool:
        now = time.time()
        with self._lock:
            recent = [t for t in self._failures.get(username, []) if now - t < self.lockout_s]
            self._failures[username] = recent
            return len(recent) >= self.max_failures

    def fail(self, username: str) -> None:
        with self._lock:
            self._failures.setdefault(username, []).append(time.time())

    def reset(self, username: str) -> None:
        with self._lock:
            self._failures.pop(username, None)
