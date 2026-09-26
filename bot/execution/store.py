"""ExecutionStore: SQLite (WAL) persistence for the V1.5 runtime (spec §10).

Tables: orders, fills, positions (module slices), risk_state, decisions, heartbeats, throttles, protective_orders,
watermarks, trades, meta. Every submit is two transactions: the ``submitting`` row is committed BEFORE the broker call
and updated after it, so a crash between the two leaves a row that reconciliation resolves through the deterministic
client_order_id (never a second submission). ``migrate_from_json`` imports the V1 JSON state file; the JSON reader stays
for one release.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orders (client_order_id TEXT PRIMARY KEY, broker_id TEXT, module TEXT, symbol TEXT, side TEXT, qty REAL, kind TEXT,
    style TEXT, session TEXT, decision_ts TEXT, reference_price REAL, status TEXT, filled_qty REAL DEFAULT 0, avg_price REAL, tif TEXT,
    limit_price REAL, stop_price REAL, protective_for TEXT, abandon_at TEXT, repriced INTEGER DEFAULT 0, events TEXT, risk TEXT,
    created_at TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS ix_orders_status ON orders(status);
CREATE INDEX IF NOT EXISTS ix_orders_broker ON orders(broker_id);
CREATE TABLE IF NOT EXISTS fills (id INTEGER PRIMARY KEY, order_id TEXT, client_order_id TEXT, event TEXT, ts TEXT, symbol TEXT, side TEXT,
    qty REAL, filled_qty REAL, price REAL, status TEXT, UNIQUE(order_id, event, ts));
CREATE TABLE IF NOT EXISTS positions (module TEXT, symbol TEXT, qty REAL, avg_price REAL, opened_session TEXT, protective_order_id TEXT,
    unprotected INTEGER DEFAULT 0, unprotected_overnight INTEGER DEFAULT 0, updated_at TEXT, PRIMARY KEY(module, symbol));
CREATE TABLE IF NOT EXISTS risk_state (id INTEGER PRIMARY KEY CHECK (id = 1), json TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS decisions (id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, module TEXT, symbol TEXT, decision TEXT, detail TEXT, json TEXT);
CREATE TABLE IF NOT EXISTS heartbeats (component TEXT PRIMARY KEY, ts TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS heartbeat_log (id INTEGER PRIMARY KEY, component TEXT, ts TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS throttles (module TEXT PRIMARY KEY, multiplier REAL, reason TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS protective_orders (module TEXT, symbol TEXT, client_order_id TEXT, stop_price REAL, tif TEXT, ts TEXT, PRIMARY KEY(module, symbol));
CREATE TABLE IF NOT EXISTS watermarks (name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, module TEXT, symbol TEXT, qty REAL, entry_price REAL, exit_price REAL, pnl REAL,
    exit_reason TEXT, exit_ts TEXT, session TEXT);
"""


def _iso(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v)


class ExecutionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.path), isolation_level=None, check_same_thread=False)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(_SCHEMA)
        if self.meta("schema_version") is None:
            self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self) -> None:
        self.con.close()

    # ------------------------------------------------------------------ meta
    def meta(self, key: str) -> str | None:
        r = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.con.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    # ---------------------------------------------------------------- orders
    def _order_row(self, rec) -> tuple:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return (rec.client_order_id, rec.broker_id, rec.module_id, rec.symbol, rec.side, rec.qty, rec.kind, rec.style, _iso(rec.session), _iso(rec.decision_ts),
                rec.reference_price, rec.status, rec.filled_qty, rec.avg_price, rec.tif, rec.limit_price, rec.stop_price, rec.protective_for,
                _iso(rec.abandon_at), int(rec.repriced), json.dumps(rec.events), json.dumps(rec.risk, default=str) if rec.risk else None, now, now)

    def begin_submit(self, rec) -> None:
        """Commit the order as ``submitting`` BEFORE calling the broker."""
        row = self._order_row(rec)
        with self.con:
            self.con.execute("INSERT OR REPLACE INTO orders VALUES (" + ",".join("?" * 24) + ")", row)

    def update_order(self, rec) -> None:
        row = self._order_row(rec)
        with self.con:
            self.con.execute("INSERT INTO orders VALUES (" + ",".join("?" * 24) + ") ON CONFLICT(client_order_id) DO UPDATE SET broker_id=excluded.broker_id, "
                             "status=excluded.status, filled_qty=excluded.filled_qty, avg_price=excluded.avg_price, limit_price=excluded.limit_price, "
                             "stop_price=excluded.stop_price, protective_for=excluded.protective_for, abandon_at=excluded.abandon_at, repriced=excluded.repriced, "
                             "events=excluded.events, risk=COALESCE(excluded.risk, orders.risk), updated_at=excluded.updated_at", row)

    def orders(self, *, status: str | None = None, open_only: bool = False) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM orders", []
        if status:
            q += " WHERE status=?"; args.append(status)
        elif open_only:
            q += " WHERE status IN ('submitting','new','accepted','partially_filled','pending_new','held')"
        cur = self.con.execute(q + " ORDER BY created_at", args)
        cols = [c[0] for c in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["events"] = json.loads(d["events"]) if d["events"] else []
            d["risk"] = json.loads(d["risk"]) if d["risk"] else None
            out.append(d)
        return out

    def submitting_orders(self) -> list[dict[str, Any]]:
        """Rows committed before a broker call whose outcome was never recorded (crash mid-submission)."""
        return self.orders(status="submitting")

    # ----------------------------------------------------------------- fills
    def record_fill(self, ev) -> bool:
        """Idempotent by (order_id, event, ts). Returns True when the event is new."""
        try:
            with self.con:
                self.con.execute("INSERT INTO fills (order_id, client_order_id, event, ts, symbol, side, qty, filled_qty, price, status) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (ev.order_id or ev.client_order_id, ev.client_order_id, ev.event, _iso(ev.ts), ev.symbol, ev.side, ev.qty, ev.filled_qty, ev.price, ev.status))
            return True
        except sqlite3.IntegrityError:
            return False

    def seen_events(self) -> set[tuple[str, str, str]]:
        return {(r[0], r[1], r[2]) for r in self.con.execute("SELECT order_id, event, ts FROM fills").fetchall()}

    def fills(self, *, since: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM fills", []
        if since:
            q += " WHERE ts >= ?"; args.append(since)
        cur = self.con.execute(q + " ORDER BY id", args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------- positions
    def save_slices(self, ledger) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.con:
            self.con.execute("DELETE FROM positions")
            for (m, s), sl in ledger.slices.items():
                if abs(sl.qty) > 1e-12:
                    self.con.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?)",
                                     (m, s, sl.qty, sl.avg_price, _iso(sl.opened_session), sl.protective_order_id, int(sl.unprotected), int(sl.unprotected_overnight), now))

    def load_slices(self, ledger) -> int:
        n = 0
        for m, s, qty, avg, opened, prot, unp, unpo, _ in self.con.execute("SELECT * FROM positions").fetchall():
            sl = ledger.slice(m, s)
            sl.qty, sl.avg_price = float(qty), float(avg or 0.0)
            sl.opened_session = date.fromisoformat(opened) if opened else None
            sl.protective_order_id, sl.unprotected, sl.unprotected_overnight = prot, bool(unp), bool(unpo)
            n += 1
        return n

    def positions(self) -> list[dict[str, Any]]:
        cur = self.con.execute("SELECT * FROM positions")
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------------ risk
    def save_risk(self, state) -> None:
        d = state.to_dict() if hasattr(state, "to_dict") else dict(state)
        with self.con:
            self.con.execute("INSERT INTO risk_state (id, json, updated_at) VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET json=excluded.json, updated_at=excluded.updated_at",
                             (json.dumps(d, default=str), datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def load_risk(self) -> dict[str, Any] | None:
        r = self.con.execute("SELECT json FROM risk_state WHERE id=1").fetchone()
        return json.loads(r[0]) if r else None

    # ------------------------------------------------------------- decisions
    def add_decision(self, d: dict[str, Any], *, kind: str = "order") -> None:
        with self.con:
            self.con.execute("INSERT INTO decisions (ts, kind, module, symbol, decision, detail, json) VALUES (?,?,?,?,?,?,?)",
                             (str(d.get("ts", "")), kind, d.get("module"), d.get("symbol"), d.get("decision"), str(d.get("detail", ""))[:500], json.dumps(d, default=str)))

    def decisions(self, *, kind: str | None = None, limit: int = 1000) -> list[dict[str, Any]]:
        q, args = "SELECT json FROM decisions", []
        if kind:
            q += " WHERE kind=?"; args.append(kind)
        q += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        return [json.loads(r[0]) for r in self.con.execute(q, args).fetchall()]

    # ------------------------------------------------------------ heartbeats
    def heartbeat(self, component: str, ts: datetime | None = None, detail: str = "") -> None:
        t = _iso(ts or datetime.now(timezone.utc))
        with self.con:
            self.con.execute("INSERT INTO heartbeats VALUES (?,?,?) ON CONFLICT(component) DO UPDATE SET ts=excluded.ts, detail=excluded.detail", (component, t, detail))
            self.con.execute("INSERT INTO heartbeat_log (component, ts, detail) VALUES (?,?,?)", (component, t, detail))

    def heartbeats(self) -> dict[str, tuple[str, str]]:
        return {c: (t, d) for c, t, d in self.con.execute("SELECT component, ts, detail FROM heartbeats").fetchall()}

    def heartbeat_log(self, component: str | None = None) -> list[tuple[str, str, str]]:
        if component:
            return self.con.execute("SELECT component, ts, detail FROM heartbeat_log WHERE component=? ORDER BY id", (component,)).fetchall()
        return self.con.execute("SELECT component, ts, detail FROM heartbeat_log ORDER BY id").fetchall()

    # ------------------------------------------------------------- throttles
    def set_throttle(self, module: str, multiplier: float, reason: str) -> None:
        with self.con:
            self.con.execute("INSERT INTO throttles VALUES (?,?,?,?) ON CONFLICT(module) DO UPDATE SET multiplier=excluded.multiplier, reason=excluded.reason, ts=excluded.ts",
                             (module, multiplier, reason, datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def clear_throttle(self, module: str) -> bool:
        with self.con:
            cur = self.con.execute("DELETE FROM throttles WHERE module=?", (module,))
        return cur.rowcount > 0

    def throttles(self) -> dict[str, tuple[float, str]]:
        return {m: (float(x), r) for m, x, r, _ in self.con.execute("SELECT * FROM throttles").fetchall()}

    # ------------------------------------------------------------ protective
    def set_protective(self, module: str, symbol: str, client_order_id: str | None, stop_price: float | None, tif: str | None) -> None:
        with self.con:
            if client_order_id is None:
                self.con.execute("DELETE FROM protective_orders WHERE module=? AND symbol=?", (module, symbol))
            else:
                self.con.execute("INSERT INTO protective_orders VALUES (?,?,?,?,?,?) ON CONFLICT(module, symbol) DO UPDATE SET client_order_id=excluded.client_order_id, "
                                 "stop_price=excluded.stop_price, tif=excluded.tif, ts=excluded.ts",
                                 (module, symbol, client_order_id, stop_price, tif, datetime.now(timezone.utc).isoformat(timespec="seconds")))

    def protectives(self) -> dict[tuple[str, str], dict[str, Any]]:
        return {(m, s): {"client_order_id": c, "stop_price": sp, "tif": t, "ts": ts} for m, s, c, sp, t, ts in self.con.execute("SELECT * FROM protective_orders").fetchall()}

    # ------------------------------------------------------------ watermarks
    def watermark(self, name: str) -> str | None:
        r = self.con.execute("SELECT value FROM watermarks WHERE name=?", (name,)).fetchone()
        return r[0] if r else None

    def set_watermark(self, name: str, value: Any) -> None:
        with self.con:
            self.con.execute("INSERT INTO watermarks VALUES (?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (name, _iso(value)))

    # ---------------------------------------------------------------- trades
    def add_trade(self, t: dict[str, Any]) -> None:
        with self.con:
            self.con.execute("INSERT INTO trades (module, symbol, qty, entry_price, exit_price, pnl, exit_reason, exit_ts, session) VALUES (?,?,?,?,?,?,?,?,?)",
                             (t.get("module"), t.get("symbol"), t.get("qty"), t.get("entry_price"), t.get("exit_price"), t.get("pnl"), t.get("exit_reason"),
                              _iso(t.get("exit_ts")), _iso(t.get("session"))))

    def trades(self, module: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM trades", []
        if module:
            q += " WHERE module=?"; args.append(module)
        cur = self.con.execute(q + " ORDER BY id", args)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    # ------------------------------------------------------------- migration
    def migrate_from_json(self, json_path: str | Path, *, module_id: str | None = None) -> dict[str, int]:
        """Import a V1 JSON state file (orders, positions, risk, trades). Idempotent: existing rows win."""
        from bot.execution.state import StateStore
        st = StateStore(json_path).load()
        mod = module_id or st.strategy or "legacy"
        n_orders = n_pos = n_trades = 0
        with self.con:
            for cid, o in st.orders.items():
                if self.con.execute("SELECT 1 FROM orders WHERE client_order_id=?", (cid,)).fetchone():
                    continue
                now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                self.con.execute("INSERT INTO orders (client_order_id, broker_id, module, symbol, side, qty, kind, style, session, decision_ts, reference_price, status, "
                                 "filled_qty, avg_price, tif, events, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (cid, o.get("broker_id") or o.get("id"), mod, o.get("symbol"), o.get("side"), float(o.get("qty") or 0), o.get("kind") or o.get("reason", "entry"),
                                  "legacy", o.get("session") or o.get("bar_date"), o.get("submitted_at") or o.get("ts"), float(o.get("reference_price") or o.get("price") or 0),
                                  o.get("status", "unknown"), float(o.get("filled_qty") or 0), o.get("filled_avg_price"), o.get("tif", "opg"), json.dumps(["migrated from json"]), now, now))
                n_orders += 1
            for sym, p in st.positions.items():
                if self.con.execute("SELECT 1 FROM positions WHERE module=? AND symbol=?", (mod, sym)).fetchone():
                    continue
                qty = float(p.get("qty") or 0) * (1 if int(p.get("side", 1)) >= 0 else -1)
                self.con.execute("INSERT INTO positions VALUES (?,?,?,?,?,?,?,?,?)", (mod, sym, qty, float(p.get("entry_price") or p.get("avg_price") or 0),
                                 p.get("entry_date") or p.get("opened_session"), None, 1, 1, datetime.now(timezone.utc).isoformat(timespec="seconds")))
                n_pos += 1
            for t in st.trades:
                self.con.execute("INSERT INTO trades (module, symbol, qty, entry_price, exit_price, pnl, exit_reason, exit_ts, session) VALUES (?,?,?,?,?,?,?,?,?)",
                                 (mod, t.get("symbol"), t.get("qty"), t.get("entry_price"), t.get("exit_price"), t.get("pnl"), t.get("exit_reason"), _iso(t.get("exit_ts")), t.get("session")))
                n_trades += 1
            if st.risk and self.load_risk() is None:
                self.save_risk(st.risk)
            self.set_meta("migrated_from", str(json_path))
            self.set_meta("run_id", st.run_id)
            if st.env:
                self.set_meta("env", st.env)
        return {"orders": n_orders, "positions": n_pos, "trades": n_trades}
