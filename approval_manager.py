"""
Approval queue: every candidate that passes risk_manager gates lands here
as PENDING, gets alerted via Telegram, and waits for a tap. Expired
requests are auto-rejected since option prices move.
"""

import sqlite3
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta

log = logging.getLogger("approval")

SCHEMA = """
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    strategy TEXT,
    candidate_json TEXT,
    status TEXT DEFAULT 'PENDING',   -- PENDING, APPROVED, REJECTED, EXPIRED, EXECUTED, FAILED
    created_at TEXT,
    expires_at TEXT,
    decided_at TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    approval_id TEXT,
    symbol TEXT,
    strategy TEXT,
    order_json TEXT,
    order_id TEXT,
    status TEXT,
    created_at TEXT
);
"""


def _normalize_turso_url_and_token(url: str):
    """Accepts libsql://..., https://..., http://... URLs.
    Returns (https_base_url, auth_token). Auth token can come from
    the `authToken` query param or from the DATABASE_AUTH_TOKEN env var
    (env takes precedence)."""
    from urllib.parse import urlparse, parse_qs, urlunparse

    token = os.environ.get("DATABASE_AUTH_TOKEN")
    parsed = urlparse(url)
    scheme = parsed.scheme
    if scheme == "libsql":
        scheme = "https"
    query_params = parse_qs(parsed.query)
    if not token and "authToken" in query_params:
        token = query_params["authToken"][0]
    cleaned = parsed._replace(scheme=scheme, query="")
    base = urlunparse(cleaned).rstrip("/")
    if not base:
        base = url
    return base, token


class _TursoHttpCursor:
    """sqlite3.Cursor-like object returned by TursoHttpConnection.execute().
    Supports .fetchone(), .fetchall(), and .rowcount (for write queries).
    Rows are tuples accessible by integer index (row[0], row[1], ...).
    """

    def __init__(self, cols=None, rows=None, rowcount=-1):
        self._cols = cols or []
        self._rows = [tuple(r) for r in (rows or [])]
        self._pos = 0
        self.rowcount = rowcount

    def fetchone(self):
        if self._pos >= len(self._rows):
            return None
        r = self._rows[self._pos]
        self._pos += 1
        return r

    def fetchall(self):
        rest = self._rows[self._pos:]
        self._pos = len(self._rows)
        return list(rest)

    def __iter__(self):
        return iter(self.fetchall())


class _TursoHttpConnection:
    """A sqlite3.Connection-compatible client that speaks pure HTTPS to
    Turso's Hrana v2 /v2/pipeline endpoint. No Rust/native code required.

    Supports the tiny subset the bot uses:
      .execute(sql, params=())  -> cursor with fetchone/fetchall/rowcount
      .executescript(sql)       -> runs multiple ;-separated statements
      .commit()                 -> no-op (each request is a transaction)
    """

    JSON_NULL = {"type": "null"}

    def __init__(self, base_url: str, auth_token: str | None = None, timeout: float = 30.0):
        import requests as _requests
        self._requests = _requests
        self._pipeline_url = f"{base_url}/v2/pipeline"
        self._headers = {"Content-Type": "application/json"}
        if auth_token:
            self._headers["Authorization"] = f"Bearer {auth_token}"
        self._timeout = timeout

    @classmethod
    def _pyval_to_arg(cls, v):
        if v is None:
            return cls.JSON_NULL
        if isinstance(v, bool):
            return {"type": "integer", "value": str(int(v))}
        if isinstance(v, int):
            return {"type": "integer", "value": str(v)}
        if isinstance(v, float):
            return {"type": "float", "value": v}
        if isinstance(v, (bytes, bytearray)):
            import base64
            return {"type": "blob", "base64": base64.b64encode(v).decode("ascii")}
        return {"type": "text", "value": str(v)}

    @classmethod
    def _cell_to_pyval(cls, cell):
        if cell is None or cell.get("type") == "null":
            return None
        t = cell.get("type")
        if t == "integer":
            try:
                return int(cell["value"])
            except (KeyError, ValueError):
                return cell.get("value")
        if t == "float":
            return float(cell.get("value", 0))
        if t == "text":
            return cell.get("value")
        if t == "blob":
            import base64
            return base64.b64decode(cell.get("base64", ""))
        return cell.get("value")

    def _run(self, statements: list) -> list:
        """statements: list of {"sql": str, "args": list or None}.
        Returns list of results (one per statement):
            for SELECT-style: {"cols": [...], "rows": [...]}
            for WRITE-style:  {"rowcount": N}
        """
        requests_payload = []
        for stmt in statements:
            entry = {
                "type": "execute",
                "stmt": {"sql": stmt["sql"]},
            }
            args = stmt.get("args")
            if args:
                entry["stmt"]["args"] = [self._pyval_to_arg(a) for a in args]
            requests_payload.append(entry)

        body = {"requests": requests_payload, "baton": None}
        try:
            resp = self._requests.post(
                self._pipeline_url,
                headers=self._headers,
                json=body,
                timeout=self._timeout,
            )
        except Exception as e:
            raise sqlite3.OperationalError(f"Turso HTTP request failed: {e}") from e

        if resp.status_code >= 400:
            msg = f"Turso HTTP {resp.status_code}: {resp.text[:500]}"
            # Map common HTTP errors to sqlite3 exception types so callers'
            # existing try/except blocks (e.g., duplicate-column migration)
            # work transparently.
            lower = resp.text.lower()
            if "duplicate column" in lower or "already exists" in lower:
                raise sqlite3.OperationalError(msg)
            if "no such" in lower or "does not exist" in lower:
                raise sqlite3.OperationalError(msg)
            raise sqlite3.DatabaseError(msg)
        try:
            data = resp.json()
        except ValueError as e:
            raise sqlite3.DatabaseError(
                f"Turso HTTP returned non-JSON body: {resp.text[:200]}"
            ) from e
        results = data.get("results") or []
        if len(results) != len(statements):
            raise sqlite3.DatabaseError(
                f"Turso pipeline result count mismatch: expected {len(statements)}, got {len(results)}"
            )

        out: list = []
        for raw in results:
            if raw.get("type") != "ok":
                err = raw.get("error", {}) if isinstance(raw, dict) else {}
                msg = f"Turso statement error: {err.get('message') or raw}"
                lower = msg.lower()
                if "duplicate column" in lower or "already exists" in lower:
                    raise sqlite3.OperationalError(msg)
                if "no such" in lower or "does not exist" in lower:
                    raise sqlite3.OperationalError(msg)
                raise sqlite3.DatabaseError(msg)
            resp_result = raw.get("response", {}).get("result", {})
            cols = [c.get("name") for c in resp_result.get("cols", [])]
            raw_rows = resp_result.get("rows", [])
            rowcount_raw = resp_result.get("affected_row_count")
            rows = []
            for raw_row in raw_rows:
                rows.append([self._cell_to_pyval(cell) for cell in raw_row])
            entry: dict = {"cols": cols, "rows": rows, "rowcount": -1}
            if rowcount_raw is not None:
                try:
                    entry["rowcount"] = int(rowcount_raw)
                except (TypeError, ValueError):
                    pass
            out.append(entry)
        return out

    def execute(self, sql: str, params=None) -> _TursoHttpCursor:
        statements = [{"sql": sql, "args": list(params) if params else []}]
        results = self._run(statements)
        r = results[0]
        return _TursoHttpCursor(cols=r["cols"], rows=r["rows"], rowcount=r.get("rowcount", -1))

    def executescript(self, script: str) -> _TursoHttpCursor:
        statements = []
        for part in script.split(";"):
            s = part.strip()
            if not s:
                continue
            statements.append({"sql": s, "args": []})
        if not statements:
            return _TursoHttpCursor()
        results = self._run(statements)
        last = results[-1]
        return _TursoHttpCursor(cols=last["cols"], rows=last["rows"], rowcount=last.get("rowcount", -1))

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


def _connect(path_or_url: str):
    """Connect to SQLite file, or to a Turso DATABASE_URL over pure HTTPS
    (no Rust / libsql binary required).

    Supported DATABASE_URL formats:
      - libsql://<db>-<org>.turso.io?authToken=XXXX   (Turso native scheme)
      - https://<db>-<org>.turso.io                   (+ DATABASE_AUTH_TOKEN env)
      - http://...                                    (local dev)

    Turso persistence is optional — if DATABASE_URL is not set (or the
    connection fails for any reason), we fall back to a local SQLite file
    so the bot still boots.
    """
    raw_url = os.environ.get("DATABASE_URL")
    if raw_url and (
        raw_url.startswith("libsql://")
        or raw_url.startswith("http://")
        or raw_url.startswith("https://")
    ):
        try:
            base_url, token = _normalize_turso_url_and_token(raw_url)
            log.info("Using Turso for persistence (HTTP): %s", base_url)
            return _TursoHttpConnection(base_url=base_url, auth_token=token)
        except Exception as e:
            log.warning(
                "Turso connection failed (DATABASE_URL=%s…), falling back to local SQLite: %s",
                raw_url[:32],
                e,
            )
    return sqlite3.connect(path_or_url, check_same_thread=False)


class Db:
    def __init__(self, path="./bot.db"):
        self.path = path
        self.conn = _connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def create_approval(self, candidate: dict, timeout_minutes: int) -> str:
        approval_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        expires = now + timedelta(minutes=timeout_minutes)
        self.conn.execute(
            "INSERT INTO approvals (id, symbol, strategy, candidate_json, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (approval_id, candidate["symbol"], candidate["strategy"],
             json.dumps(candidate), now.isoformat(), expires.isoformat()),
        )
        self.conn.commit()
        return approval_id

    def get_approval(self, approval_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT id, symbol, strategy, candidate_json, status, expires_at FROM approvals WHERE id=?",
            (approval_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "symbol": row[1], "strategy": row[2],
            "candidate": json.loads(row[3]), "status": row[4], "expires_at": row[5],
        }

    def set_status(self, approval_id: str, status: str):
        self.conn.execute(
            "UPDATE approvals SET status=?, decided_at=? WHERE id=?",
            (status, datetime.now(timezone.utc).isoformat(), approval_id),
        )
        self.conn.commit()

    def expire_stale(self):
        now = datetime.now(timezone.utc).isoformat()
        cur = self.conn.execute(
            "UPDATE approvals SET status='EXPIRED' WHERE status='PENDING' AND expires_at < ?",
            (now,),
        )
        self.conn.commit()
        return cur.rowcount

    def count_trades_today(self, day_iso: str) -> int:
        # Only count trades that actually entered execution flow and are
        # in a "consumed the cap" state: SUBMITTED, APPROVED, EXECUTED.
        # Explicitly exclude FAILED (broker rejected them, give the slot back)
        # and CANCELLED/REJECTED, so a day of order-reject bugs doesn't
        # permanently block the bot with a zero-success cap.
        row = self.conn.execute(
            "SELECT COUNT(*) FROM trades WHERE created_at LIKE ? "
            "AND status NOT IN ('FAILED','REJECTED','CANCELLED')",
            (f"{day_iso}%",),
        ).fetchone()
        return row[0] if row else 0

    def record_trade(self, approval_id, symbol, strategy, order_json, order_id_val, status):
        trade_id = str(uuid.uuid4())
        self.conn.execute(
            "INSERT INTO trades (id, approval_id, symbol, strategy, order_json, order_id, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, approval_id, symbol, strategy, json.dumps(order_json), order_id_val,
             status, datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return trade_id

    def open_position_risk_by_sector(self, sector_map: dict) -> dict:
        rows = self.conn.execute(
            "SELECT a.candidate_json FROM trades t JOIN approvals a ON t.approval_id = a.id "
            "WHERE t.status='EXECUTED' AND (t.outcome='OPEN' OR t.outcome IS NULL)"
        ).fetchall()
        by_sector = {}
        for (candidate_json,) in rows:
            c = json.loads(candidate_json)
            sector = sector_map.get(c.get("symbol", "").upper(), "Unknown")
            risk = (c.get("max_loss_per_contract") or 0) * (c.get("suggested_qty") or 1)
            by_sector[sector] = by_sector.get(sector, 0.0) + risk
        return by_sector