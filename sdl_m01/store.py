"""Transactional, content-checked storage for Module 01.

The database belongs to a trusted custodian. Hashes and SQL triggers provide
consistency checks; they do not protect against an administrator rewriting it.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
from datetime import datetime, timezone

from .errors import AccessDenied, IntegrityError, StateError, ValidationError


ROLES = ('custodian', 'explorer', 'confirmer', 'auditor')


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError('Inputs must contain finite, JSON-compatible values.') from exc


def digest(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def new_id(prefix):
    return prefix + '_' + secrets.token_hex(16)


def connect(path):
    path = Path(path)
    if not path.is_file():
        raise StateError('Vault does not exist; initialize it first.')
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db


def append_event(db, actor, action, details):
    previous = db.execute('SELECT seq,event_hash FROM ledger ORDER BY seq DESC LIMIT 1').fetchone()
    event = {'seq': previous['seq'] + 1 if previous else 1, 'at': now(),
             'actor': actor, 'action': action, 'details': details,
             'prev_hash': previous['event_hash'] if previous else '0' * 64}
    event_hash = digest(canonical(event))
    db.execute('INSERT INTO ledger VALUES (?,?,?,?,?,?,?)',
               (event['seq'], event['at'], actor, action, canonical(details),
                event['prev_hash'], event_hash))
    return event['seq']


def snapshot(db, protocol_id, kind, payload):
    body = canonical(payload)
    snapshot_id = new_id('snap')
    db.execute('INSERT INTO snapshots VALUES (?,?,?,?,?,?)',
               (snapshot_id, protocol_id, kind, body, digest(body), now()))
    return snapshot_id


def load_snapshot(db, snapshot_id):
    row = db.execute('SELECT payload,digest FROM snapshots WHERE id=?', (snapshot_id,)).fetchone()
    if row is None or not secrets.compare_digest(digest(row['payload']), row['digest']):
        raise IntegrityError('Snapshot content does not match its registered digest.')
    try:
        return json.loads(row['payload'])
    except (TypeError, ValueError) as exc:
        raise IntegrityError('Snapshot is not valid JSON.') from exc


SCHEMA = """
CREATE TABLE credentials (token_hash TEXT PRIMARY KEY, role TEXT NOT NULL);
CREATE TABLE protocols (
 id TEXT PRIMARY KEY, version INTEGER NOT NULL, spec TEXT NOT NULL,
 public TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE snapshots (
 id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES protocols(id),
 kind TEXT NOT NULL, payload TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE datasets (
 ref TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES protocols(id),
 purpose TEXT NOT NULL, state TEXT NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES snapshots(id), quality TEXT NOT NULL,
 binding_id TEXT, UNIQUE(protocol_id,purpose),
 CHECK(state IN ('open','sealed','bound_to_frozen_protocol','used','historical','compromised'))
);
CREATE TABLE bindings (
 id TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES protocols(id),
 dataset_ref TEXT NOT NULL UNIQUE REFERENCES datasets(ref),
 plan TEXT NOT NULL, plan_digest TEXT NOT NULL, alpha REAL NOT NULL,
 round_index INTEGER NOT NULL, released INTEGER NOT NULL DEFAULT 0,
 UNIQUE(protocol_id,round_index)
);
CREATE TABLE evaluations (
 seq INTEGER PRIMARY KEY AUTOINCREMENT, binding_id TEXT NOT NULL REFERENCES bindings(id),
 payload TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE identities (
 fingerprint TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES protocols(id),
 dataset_ref TEXT NOT NULL REFERENCES datasets(ref)
);
CREATE TABLE units (
 unit_key TEXT PRIMARY KEY, protocol_id TEXT NOT NULL REFERENCES protocols(id), latest_time TEXT
);
CREATE TABLE ledger (
 seq INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
 details TEXT NOT NULL, prev_hash TEXT NOT NULL, event_hash TEXT NOT NULL
);
CREATE TRIGGER snapshots_no_update BEFORE UPDATE ON snapshots
 BEGIN SELECT RAISE(ABORT,'snapshots are immutable'); END;
CREATE TRIGGER snapshots_no_delete BEFORE DELETE ON snapshots
 BEGIN SELECT RAISE(ABORT,'snapshots are immutable'); END;
CREATE TRIGGER protocols_no_update BEFORE UPDATE ON protocols
 BEGIN SELECT RAISE(ABORT,'protocols are immutable'); END;
CREATE TRIGGER protocols_no_delete BEFORE DELETE ON protocols
 BEGIN SELECT RAISE(ABORT,'protocols are immutable'); END;
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger
 BEGIN SELECT RAISE(ABORT,'ledger is append only'); END;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger
 BEGIN SELECT RAISE(ABORT,'ledger is append only'); END;
CREATE TRIGGER evaluations_no_update BEFORE UPDATE ON evaluations
 BEGIN SELECT RAISE(ABORT,'evaluations are append only'); END;
CREATE TRIGGER evaluations_no_delete BEFORE DELETE ON evaluations
 BEGIN SELECT RAISE(ABORT,'evaluations are append only'); END;
CREATE TRIGGER binding_plan_no_update BEFORE UPDATE OF
 protocol_id,dataset_ref,plan,plan_digest,alpha,round_index ON bindings
 BEGIN SELECT RAISE(ABORT,'frozen plans are immutable'); END;
CREATE TRIGGER bindings_no_delete BEFORE DELETE ON bindings
 BEGIN SELECT RAISE(ABORT,'bindings are immutable'); END;
"""


def initialize(db_path):
    """Create a vault and return role tokens once; never overwrite a vault."""
    path = Path(db_path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
    except FileExistsError as exc:
        raise StateError('Refusing to overwrite an existing vault.') from exc
    keys = {role: secrets.token_urlsafe(32) for role in ROLES}
    db = connect(path)
    try:
        db.executescript(SCHEMA)
        db.execute('BEGIN IMMEDIATE')
        for role, token in keys.items():
            db.execute('INSERT INTO credentials VALUES (?,?)', (digest(token), role))
        append_event(db, 'system', 'initialize', {'schema_version': 1})
        db.commit()
    finally:
        db.close()
    return keys


def authenticate(db, token):
    if not isinstance(token, str) or not token:
        raise AccessDenied('A valid role token is required.')
    row = db.execute('SELECT role FROM credentials WHERE token_hash=?', (digest(token),)).fetchone()
    if row is None or row['role'] not in ROLES:
        raise AccessDenied('Invalid role token.')
    return row['role']
