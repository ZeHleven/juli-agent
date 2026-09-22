"""Explicit, transactional migrations for a new Juli database only."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from contextvars import ContextVar

from juli.core.common import DomainError

APPLICATION_ID = 0x4A554C49  # JULI, does not identify any original application.
_deadline = ContextVar('juli_sqlite_deadline', default=None)


@contextmanager
def operation_deadline(seconds: float):
    previous = _deadline.get()
    deadline = time.monotonic() + seconds
    marker = _deadline.set(min(previous, deadline) if previous is not None else deadline)
    try:
        yield
    finally:
        _deadline.reset(marker)


def deadline_expired() -> bool:
    return _deadline.get() is not None and time.monotonic() >= _deadline.get()
DDL = (
    "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL)",
    "CREATE TABLE principals (id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL CHECK(enabled IN (0,1)), created_at REAL NOT NULL)",
    "CREATE TABLE tokens (id TEXT PRIMARY KEY, principal_id TEXT NOT NULL REFERENCES principals(id), token_hash TEXT NOT NULL UNIQUE, expires_at REAL NOT NULL, revoked INTEGER NOT NULL DEFAULT 0 CHECK(revoked IN (0,1)), created_at REAL NOT NULL)",
    "CREATE TABLE workspaces (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES principals(id), name TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE TABLE memberships (workspace_id TEXT NOT NULL REFERENCES workspaces(id), principal_id TEXT NOT NULL REFERENCES principals(id), role TEXT NOT NULL CHECK(role IN ('owner','editor','viewer')), PRIMARY KEY(workspace_id,principal_id))",
    "CREATE TABLE resources (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)), created_at REAL NOT NULL, UNIQUE(id,workspace_id))",
    "CREATE TABLE resource_versions (id TEXT PRIMARY KEY, resource_id TEXT NOT NULL, workspace_id TEXT NOT NULL, version_number INTEGER NOT NULL CHECK(version_number>0), descriptor_json TEXT NOT NULL, content TEXT NOT NULL, created_at REAL NOT NULL, UNIQUE(resource_id,version_number), UNIQUE(id,workspace_id), FOREIGN KEY(resource_id,workspace_id) REFERENCES resources(id,workspace_id))",
    "CREATE TABLE conversations (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL REFERENCES workspaces(id), created_by TEXT NOT NULL REFERENCES principals(id), created_at REAL NOT NULL, UNIQUE(id,workspace_id))",
    "CREATE TABLE scenario_packages (fingerprint TEXT PRIMARY KEY, scenario_id TEXT NOT NULL, version TEXT NOT NULL, package_json TEXT NOT NULL, UNIQUE(scenario_id,version))",
    "CREATE TABLE tasks (id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, conversation_id TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES principals(id), scenario_fingerprint TEXT NOT NULL REFERENCES scenario_packages(fingerprint), snapshot_json TEXT NOT NULL, snapshot_hash TEXT NOT NULL, state_json TEXT NOT NULL, state_version INTEGER NOT NULL CHECK(state_version>0), created_at REAL NOT NULL, FOREIGN KEY(conversation_id,workspace_id) REFERENCES conversations(id,workspace_id))",
    "CREATE TABLE task_resources (task_id TEXT NOT NULL REFERENCES tasks(id), version_id TEXT NOT NULL REFERENCES resource_versions(id), role TEXT NOT NULL, PRIMARY KEY(task_id,version_id,role))",
    "CREATE TABLE idempotency (principal_id TEXT NOT NULL REFERENCES principals(id), scope TEXT NOT NULL, request_key TEXT NOT NULL, request_hash TEXT NOT NULL, task_id TEXT NOT NULL REFERENCES tasks(id), PRIMARY KEY(principal_id,scope,request_key))",
    "CREATE TABLE audit_events (id INTEGER PRIMARY KEY AUTOINCREMENT, principal_id TEXT NOT NULL REFERENCES principals(id), workspace_id TEXT REFERENCES workspaces(id), action TEXT NOT NULL, target_id TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE INDEX tasks_by_conversation ON tasks(workspace_id,conversation_id)",
    "CREATE INDEX membership_by_principal ON memberships(principal_id,workspace_id)",
)


DDL_V2 = (
    "CREATE TABLE document_originals (version_id TEXT PRIMARY KEY REFERENCES resource_versions(id), filename TEXT NOT NULL, data BLOB NOT NULL)",
    "CREATE TABLE document_parses (version_id TEXT PRIMARY KEY REFERENCES resource_versions(id), parser_version TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('ready','failed')), error_code TEXT, extracted_hash TEXT NOT NULL, parsed_json TEXT)",
    "CREATE TABLE evidence_chunks (id TEXT PRIMARY KEY, version_id TEXT NOT NULL REFERENCES resource_versions(id), evidence_json TEXT NOT NULL)",
    "CREATE TABLE evidence_terms (term TEXT NOT NULL, evidence_id TEXT NOT NULL REFERENCES evidence_chunks(id), PRIMARY KEY(term,evidence_id))",
    "CREATE TABLE evidence_reads (task_id TEXT NOT NULL REFERENCES tasks(id), evidence_id TEXT NOT NULL REFERENCES evidence_chunks(id), principal_id TEXT NOT NULL REFERENCES principals(id), content_hash TEXT NOT NULL, created_at REAL NOT NULL, PRIMARY KEY(task_id,evidence_id,principal_id))",
    "CREATE INDEX chunks_by_version ON evidence_chunks(version_id)",
)
DDL_V3 = (
    "CREATE TABLE runtime_runs (task_id TEXT PRIMARY KEY REFERENCES tasks(id), profile TEXT NOT NULL, policy_json TEXT NOT NULL, checkpoint_json TEXT NOT NULL)",
    "CREATE TABLE runtime_locks (conversation_id TEXT PRIMARY KEY REFERENCES conversations(id), task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id))",
    "CREATE TABLE runtime_actions (id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN ('reserved','committed','unknown','cancelled')), expires_at REAL NOT NULL)",
    "CREATE UNIQUE INDEX one_reserved_action ON runtime_actions(task_id) WHERE state='reserved'",
    "CREATE TABLE runtime_events (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL REFERENCES tasks(id), kind TEXT NOT NULL, created_at REAL NOT NULL)",
    "CREATE TABLE runtime_answers (task_id TEXT NOT NULL REFERENCES tasks(id), request_key TEXT NOT NULL, request_hash TEXT NOT NULL, PRIMARY KEY(task_id,request_key))",
)
LATEST_VERSION = 3


def checksum(version: int = 1) -> str:
    statements = {1: DDL, 2: DDL_V2, 3: DDL_V3}[version]
    return hashlib.sha256(json.dumps(statements, separators=(",", ":")).encode()).hexdigest()


def connect(path: Path, *, create: bool = False) -> sqlite3.Connection:
    path = Path(path).resolve()
    # URI mode=rw forbids silently creating a database during application startup.
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + ("?mode=rwc" if create else "?mode=rw"), uri=True, timeout=5, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        if _deadline.get() is not None:
            if deadline_expired():
                db.close()
                raise DomainError('operation_timeout', 'The local operation deadline has expired.')
            milliseconds = max(1, min(5000, int((_deadline.get() - time.monotonic()) * 1000)))
            db.execute(f'PRAGMA busy_timeout={milliseconds}')
            db.set_progress_handler(lambda: int(deadline_expired()), 1000)
        return db
    except sqlite3.Error as exc:
        if db is not None:
            db.close()
        raise DomainError("storage_unavailable", "Cannot open this local store; initialize it explicitly first.") from exc


def check_schema(db: sqlite3.Connection, *, allow_previous: bool = False) -> None:
    app_id = db.execute("PRAGMA application_id").fetchone()[0]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if app_id != APPLICATION_ID or version not in ((1, 2, 3) if allow_previous else (LATEST_VERSION,)):
        raise DomainError("database_not_ready", "Use a Juli database initialized by the explicit migration command.")
    try:
        rows = db.execute("SELECT version,checksum FROM schema_migrations ORDER BY version").fetchall()
    except sqlite3.Error as exc:
        raise DomainError("database_not_ready", "The Juli schema receipt is missing.") from exc
    if [(r[0], r[1]) for r in rows] != [(v, checksum(v)) for v in range(1, version + 1)]:
        raise DomainError("migration_mismatch", "The installed migration does not match this application.")
    actual = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    statements = DDL + (DDL_V2 if version >= 2 else ()) + (DDL_V3 if version >= 3 else ())
    expected = {statement.split()[2] for statement in statements if statement.startswith("CREATE TABLE ")}
    if actual != expected:
        raise DomainError("database_not_ready", "The database table set does not match the installed revision.")


def migrate(path: Path) -> dict:
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    db = connect(path, create=True)
    try:
        db.execute("BEGIN IMMEDIATE")
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
        app_id = db.execute("PRAGMA application_id").fetchone()[0]
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if tables or app_id or version:
            check_schema(db, allow_previous=True)
        if version == LATEST_VERSION:
            db.execute("COMMIT")
            return {"version": LATEST_VERSION, "changed": False}
        if version == 0:
            for statement in DDL:
                db.execute(statement)
            db.execute("INSERT INTO schema_migrations VALUES (?,?)", (1, checksum()))
            db.execute(f"PRAGMA application_id={APPLICATION_ID}")
        if version < 2:
            for statement in DDL_V2:
                db.execute(statement)
            from .document_index import backfill_v1
            backfill_v1(db)
            db.execute("INSERT INTO schema_migrations VALUES (?,?)", (2, checksum(2)))
        for statement in DDL_V3:
            db.execute(statement)
        db.execute("INSERT INTO schema_migrations VALUES (?,?)", (3, checksum(3)))
        db.execute("PRAGMA user_version=3")
        db.execute("COMMIT")
        return {"version": LATEST_VERSION, "changed": True}
    except Exception:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()


@contextmanager
def transaction(path: Path, *, write: bool = False):
    db = connect(path)
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        check_schema(db)
        yield db
        if deadline_expired():
            raise DomainError('operation_timeout', 'The local operation deadline has expired.')
        db.execute("COMMIT")
    except sqlite3.IntegrityError as exc:
        db.set_progress_handler(None, 0)
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise DomainError("storage_conflict", "The operation conflicts with an existing record.") from exc
    except sqlite3.OperationalError as exc:
        db.set_progress_handler(None, 0)
        if db.in_transaction:
            db.execute("ROLLBACK")
        code = 'operation_timeout' if deadline_expired() else 'storage_unavailable'
        raise DomainError(code, 'The local operation could not complete.') from exc
    except Exception:
        db.set_progress_handler(None, 0)
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
