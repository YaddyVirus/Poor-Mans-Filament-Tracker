"""SQLite storage for spools, usage history, events, and app metadata."""
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

DB_PATH = os.environ.get("DB_PATH") or (
    "/data/filament.db" if os.path.isdir("/data") else "filament.db"
)

SPOOL_FIELDS = (
    "brand", "name", "material", "color_hex", "initial_weight_g",
    "remaining_g", "cost", "notes",
)


@contextmanager
def _connect():
    """sqlite3.Connection's own context manager only commits/rolls back —
    it never closes the connection, so every call site would otherwise leak
    one. Wrap it so `with _connect() as con:` also closes on the way out."""
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        with con:
            yield con
    finally:
        con.close()


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init():
    with _connect() as con:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS spools (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brand TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                material TEXT NOT NULL DEFAULT 'PLA+',
                color_hex TEXT NOT NULL DEFAULT '#808080',
                initial_weight_g REAL NOT NULL DEFAULT 1000,
                remaining_g REAL NOT NULL DEFAULT 1000,
                cost REAL,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                active INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                spool_id INTEGER NOT NULL REFERENCES spools(id) ON DELETE CASCADE,
                grams REAL NOT NULL,
                job_name TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'print',
                ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                spool_id INTEGER,
                ts TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
        """)


# ---------- meta ----------

def get_meta(key, default=None):
    with _connect() as con:
        row = con.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default


def set_meta(key, value):
    with _connect() as con:
        con.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, json.dumps(value)),
        )


# ---------- spools ----------

def list_spools(include_archived=False):
    q = "SELECT * FROM spools"
    if not include_archived:
        q += " WHERE archived = 0"
    q += " ORDER BY active DESC, created_at DESC"
    with _connect() as con:
        return [dict(r) for r in con.execute(q)]


def get_spool(spool_id):
    with _connect() as con:
        row = con.execute("SELECT * FROM spools WHERE id = ?", (spool_id,)).fetchone()
        return dict(row) if row else None


def active_spool():
    with _connect() as con:
        row = con.execute(
            "SELECT * FROM spools WHERE active = 1 AND archived = 0"
        ).fetchone()
        return dict(row) if row else None


def create_spool(data):
    fields = {f: data[f] for f in SPOOL_FIELDS if f in data}
    fields.setdefault("remaining_g", fields.get("initial_weight_g", 1000))
    fields["created_at"] = _now()
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    with _connect() as con:
        cur = con.execute(
            f"INSERT INTO spools ({cols}) VALUES ({marks})", list(fields.values())
        )
        new_id = cur.lastrowid
    return get_spool(new_id)


def update_spool(spool_id, data):
    """Update editable fields. A manual change to remaining_g is logged
    as an 'adjust' usage record so history stays honest."""
    old = get_spool(spool_id)
    if not old:
        return None
    fields = {f: data[f] for f in SPOOL_FIELDS if f in data}
    if not fields:
        return old
    sets = ", ".join(f"{k} = ?" for k in fields)
    with _connect() as con:
        con.execute(
            f"UPDATE spools SET {sets} WHERE id = ?",
            [*fields.values(), spool_id],
        )
        if "remaining_g" in fields:
            delta = old["remaining_g"] - float(fields["remaining_g"])
            if abs(delta) >= 0.05:
                con.execute(
                    "INSERT INTO usage (spool_id, grams, job_name, kind, ts)"
                    " VALUES (?, ?, ?, 'adjust', ?)",
                    (spool_id, round(delta, 2), "Manual adjustment", _now()),
                )
    return get_spool(spool_id)


def delete_spool(spool_id):
    with _connect() as con:
        con.execute("DELETE FROM spools WHERE id = ?", (spool_id,))


def set_active(spool_id):
    """Mark a spool as loaded. Logs a spool_change event when it actually
    changes — the 'prints since last filament change' buffer keys off this.
    Checked up front: clearing active flags for an id that doesn't exist
    would silently unload the current spool."""
    if not get_spool(spool_id):
        return None
    previous = active_spool()
    with _connect() as con:
        con.execute("UPDATE spools SET active = 0")
        con.execute(
            "UPDATE spools SET active = 1, archived = 0 WHERE id = ?", (spool_id,)
        )
        if not previous or previous["id"] != spool_id:
            con.execute(
                "INSERT INTO events (kind, spool_id, ts) VALUES ('spool_change', ?, ?)",
                (spool_id, _now()),
            )
    return get_spool(spool_id)


def set_archived(spool_id, archived):
    with _connect() as con:
        con.execute(
            "UPDATE spools SET archived = ?, active = CASE WHEN ? THEN 0 ELSE active END"
            " WHERE id = ?",
            (int(archived), int(archived), spool_id),
        )
    return get_spool(spool_id)


# ---------- usage ----------

def deduct(spool_id, grams, job_name, kind="print"):
    """Subtract grams from a spool (floored at 0) and record it."""
    with _connect() as con:
        con.execute(
            "UPDATE spools SET remaining_g = MAX(0, remaining_g - ?) WHERE id = ?",
            (grams, spool_id),
        )
        con.execute(
            "INSERT INTO usage (spool_id, grams, job_name, kind, ts)"
            " VALUES (?, ?, ?, ?, ?)",
            (spool_id, round(grams, 2), job_name or "", kind, _now()),
        )
    return get_spool(spool_id)


def get_usage(usage_id):
    with _connect() as con:
        row = con.execute("SELECT * FROM usage WHERE id = ?", (usage_id,)).fetchone()
        return dict(row) if row else None


def reassign_usage(usage_id, new_spool_id):
    """Move a recorded print to a different spool: refund the old spool,
    deduct the new one, repoint the record."""
    row = get_usage(usage_id)
    if not row or not get_spool(new_spool_id) or row["spool_id"] == new_spool_id:
        return row
    with _connect() as con:
        con.execute(
            "UPDATE spools SET remaining_g = remaining_g + ? WHERE id = ?",
            (row["grams"], row["spool_id"]),
        )
        con.execute(
            "UPDATE spools SET remaining_g = MAX(0, remaining_g - ?) WHERE id = ?",
            (row["grams"], new_spool_id),
        )
        con.execute(
            "UPDATE usage SET spool_id = ? WHERE id = ?", (new_spool_id, usage_id)
        )
    return get_usage(usage_id)


def update_usage_grams(usage_id, grams):
    """Correct a record's grams (e.g. weight was unknown at print time);
    the spool's remaining weight is adjusted by the difference."""
    row = get_usage(usage_id)
    if not row:
        return None
    delta = float(grams) - row["grams"]
    with _connect() as con:
        con.execute(
            "UPDATE spools SET remaining_g = MAX(0, remaining_g - ?) WHERE id = ?",
            (delta, row["spool_id"]),
        )
        con.execute(
            "UPDATE usage SET grams = ? WHERE id = ?", (round(float(grams), 2), usage_id)
        )
    return get_usage(usage_id)


def usage_history(spool_id=None, since=None, limit=200):
    q = (
        "SELECT u.*, s.brand, s.name, s.color_hex, s.material,"
        " s.cost AS spool_cost, s.initial_weight_g AS spool_initial_weight_g"
        " FROM usage u JOIN spools s ON s.id = u.spool_id WHERE 1=1"
    )
    args = []
    if spool_id:
        q += " AND u.spool_id = ?"
        args.append(spool_id)
    if since:
        q += " AND u.ts >= ? AND u.kind != 'adjust'"
        args.append(since)
    q += " ORDER BY u.ts DESC, u.id DESC LIMIT ?"
    args.append(limit)
    with _connect() as con:
        return [dict(r) for r in con.execute(q, args)]


def last_spool_change():
    with _connect() as con:
        row = con.execute(
            "SELECT ts FROM events WHERE kind = 'spool_change'"
            " ORDER BY ts DESC, id DESC LIMIT 1"
        ).fetchone()
        return row["ts"] if row else None
