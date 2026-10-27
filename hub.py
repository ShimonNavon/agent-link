#!/usr/bin/env python3
"""agent-link hub: one message bus for the Mac, the Fedora laptop and every agent on them.

Three faces, one store:
  HTTP API   /v1/*        for scripts, the CLI and anything that speaks JSON
  MCP        /mcp         Streamable HTTP, so every coding agent gets link_* tools
  Page       /            a small live status page for people

Identity comes from the URL (/mcp/<agent-id>), the X-Agent-Id header, or an explicit
"from"/"as" field. Messages are store-and-forward: a machine that is asleep loses nothing.

Standard library only. Run:  AGENT_LINK_DB=./data/link.db python3 hub.py
"""
from __future__ import annotations

import ipaddress
import json
import os
import sqlite3
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", "8080"))
DB_PATH = Path(os.environ.get("AGENT_LINK_DB", "link.db"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).resolve().parent))
TOKEN = os.environ.get("AGENT_LINK_TOKEN", "")
ALLOWED_HOSTS = {
    h.strip().lower() for h in os.environ.get("ALLOWED_HOSTS", "").split(",") if h.strip()
}
ALLOWED_HOSTS.update({"localhost", "127.0.0.1", "::1"})
ALLOWED_HOSTS.add(urllib.parse.urlsplit(PUBLIC_URL).hostname or "localhost")
VERSION = "1.0.0"
PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
PRESENCE_SECONDS = float(os.environ.get("PRESENCE_SECONDS", "300"))
KINDS = {"note", "task", "question", "answer", "handoff", "result", "status"}

INSTRUCTIONS = (
    "agent-link is the message bus between Simon's Mac, the Fedora laptop (host 'home') and the "
    "coding agents on both. Call link_whoami first to see your identity, link_agents to see who "
    "else is here, then link_send to hand work to another agent or machine and link_inbox to pick "
    "up what others sent. Messages persist until acknowledged, so the other side can be offline."
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS participants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT '',
  machine TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL DEFAULT 'agent',
  description TEXT NOT NULL DEFAULT '',
  first_seen REAL NOT NULL,
  last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  sender TEXT NOT NULL,
  recipients TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'note',
  subject TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL DEFAULT '',
  meta TEXT NOT NULL DEFAULT '{}',
  reply_to INTEGER
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS cursors (
  participant TEXT PRIMARY KEY,
  last_id INTEGER NOT NULL DEFAULT 0
);
"""


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(*parts) -> None:
    print(time.strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)


class Store:
    """SQLite behind one lock. Traffic is tiny; correctness beats cleverness."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.lock:
            self.db.executescript(SCHEMA)
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.commit()
        self.started = now()

    def touch(self, ident: str, name: str = "", machine: str = "", kind: str = "",
              description: str = "") -> dict:
        if not ident:
            raise ValueError("an identity is required")
        ident = ident.strip()
        with self.lock:
            row = self.db.execute("SELECT * FROM participants WHERE id=?", (ident,)).fetchone()
            ts = now()
            if row is None:
                self.db.execute(
                    "INSERT INTO participants(id,name,machine,kind,description,first_seen,last_seen)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (ident, name or ident, machine or ident.split("-")[0],
                     kind or ("human" if ident.endswith("simon") else "agent"),
                     description, ts, ts),
                )
            else:
                self.db.execute(
                    "UPDATE participants SET name=?,machine=?,kind=?,description=?,last_seen=?"
                    " WHERE id=?",
                    (name or row["name"], machine or row["machine"], kind or row["kind"],
                     description or row["description"], ts, ident),
                )
            self.db.commit()
            row = self.db.execute("SELECT * FROM participants WHERE id=?", (ident,)).fetchone()
        return dict(row)

    def agents(self) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM participants ORDER BY kind, id").fetchall()
            cursors = {r["participant"]: r["last_id"] for r in
                       self.db.execute("SELECT * FROM cursors").fetchall()}
        ts = now()
        out = []
        for r in rows:
            d = dict(r)
            d["online"] = (ts - d["last_seen"]) < PRESENCE_SECONDS
            d["first_seen"] = iso(d["first_seen"])
            d["last_seen"] = iso(d["last_seen"])
            d["unread"] = max(0, self._unread_after(d["id"], cursors.get(d["id"], 0), 1000))
            d["cursor"] = cursors.get(d["id"], 0)
            out.append(d)
        return out

    def _unread_after(self, ident: str, cursor: int, limit: int) -> int:
        with self.lock:
            rows = self.db.execute(
                "SELECT id,sender,recipients FROM messages WHERE id>? ORDER BY id DESC LIMIT ?",
                (cursor, limit),
            ).fetchall()
        n = 0
        for r in rows:
            recips = json.loads(r["recipients"])
            if r["sender"] != ident and ("*" in recips or ident in recips):
                n += 1
        return n

    def send(self, sender: str, to, kind: str, subject: str, body: str,
             meta: dict | None = None, reply_to: int | None = None) -> dict:
        if isinstance(to, str):
            to = [to]
        recips = sorted({str(t).strip() for t in to if str(t).strip()})
        if not recips:
            raise ValueError("'to' must name at least one recipient, or '*'")
        kind = (kind or "note").lower()
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {sorted(KINDS)}")
        payload = json.dumps(meta or {}, ensure_ascii=False)
        with self.lock:
            cur = self.db.execute(
                "INSERT INTO messages(ts,sender,recipients,kind,subject,body,meta,reply_to)"
                " VALUES(?,?,?,?,?,?,?,?)",
                (now(), sender, json.dumps(recips), kind, subject or "", body or "", payload,
                 reply_to),
            )
            self.db.commit()
            mid = cur.lastrowid
            row = self.db.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
        return self._message(row)

    def inbox(self, ident: str, unread_only: bool, limit: int, since_id: int,
              with_id: str | None) -> list[dict]:
        cursor = self.cursor(ident)
        floor = cursor if unread_only else max(since_id, 0)
        scan = max(limit * 4, 100)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM messages WHERE id>? ORDER BY id ASC LIMIT ?", (floor, scan)
            ).fetchall()
        out = []
        for r in rows:
            recips = json.loads(r["recipients"])
            if r["sender"] == ident:
                continue
            if "*" not in recips and ident not in recips:
                continue
            if with_id and with_id not in ("*", r["sender"]) and with_id not in recips:
                continue
            out.append(self._message(r))
        return out[-limit:]

    def cursor(self, ident: str) -> int:
        with self.lock:
            row = self.db.execute("SELECT last_id FROM cursors WHERE participant=?",
                                  (ident,)).fetchone()
        return row["last_id"] if row else 0

    def ack(self, ident: str, up_to: int) -> int:
        with self.lock:
            row = self.db.execute("SELECT last_id FROM cursors WHERE participant=?",
                                  (ident,)).fetchone()
            current = row["last_id"] if row else 0
            target = max(current, min(int(up_to), self._max_id()))
            self.db.execute(
                "INSERT INTO cursors(participant,last_id) VALUES(?,?)"
                " ON CONFLICT(participant) DO UPDATE SET last_id=excluded.last_id",
                (ident, target),
            )
            self.db.commit()
        return target

    def _max_id(self) -> int:
        return self.db.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0]

    def max_id(self) -> int:
        with self.lock:
            return self._max_id()

    def recent(self, limit: int = 20) -> list[dict]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM messages ORDER BY id DESC LIMIT ?",
                                   (limit,)).fetchall()
        return [self._message(r) for r in reversed(rows)]

    def status(self) -> dict:
        with self.lock:
            agents = self.db.execute("SELECT COUNT(*) FROM participants").fetchone()[0]
            messages = self.db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
        return {"ok": True, "service": "agent-link", "version": VERSION,
                "up_since": iso(self.started), "agents": agents, "messages": messages,
                "max_id": self.max_id()}

    def _message(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        d["from"] = d.pop("sender")
        d["recipients"] = json.loads(d["recipients"])
        d["meta"] = json.loads(d["meta"] or "{}")
        d["ts"] = iso(d["ts"])
        d["read"] = False
        return d


STORE: Store


def message_text(m: dict) -> str:
    """One compact line per message for CLIs and MCP results."""
    tail = f" ({m['kind']})" if m["kind"] != "note" else ""
    subj = f" — {m['subject']}" if m["subject"] else ""
    body = " ".join(m["body"].split())
    if len(body) > 500:
        body = body[:500] + "…"
    return f"[{m['id']}] {m['ts']} {m['from']} → {','.join(m['recipients'])}{tail}{subj}\n    {body}"


TOOLS = [
    {
        "name": "link_whoami",
        "description": "Who this endpoint speaks as, and the identities you can send to. Call this first.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "link_agents",
        "description": "Everyone on the bus: the Mac, the Fedora laptop and the agents on them, with "
                       "online state, last seen and unread count.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "link_send",
        "description": "Send a message to another agent, a machine or everyone ('*'). Use kind=task to "
                       "hand off work, question to ask, answer to reply, handoff to transfer a session. "
                       "Messages persist until read, so the recipient can be offline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Agent id from link_agents, or '*' for all"},
                "message": {"type": "string", "description": "The message body, plain text"},
                "subject": {"type": "string", "description": "Short one-line title"},
                "kind": {"type": "string", "enum": sorted(KINDS)},
                "reply_to": {"type": "integer", "description": "id of the message being answered"},
                "meta": {"type": "object", "description": "Optional structured payload (paths, urls)"},
                "from": {"type": "string", "description": "Override the sender identity"},
            },
            "required": ["to", "message"],
        },
    },
    {
        "name": "link_inbox",
        "description": "Messages sent to you, oldest first. Unread by default. Acknowledge with "
                       "link_ack once handled.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "unread_only": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "default": 20},
                "since_id": {"type": "integer", "default": 0},
                "with": {"type": "string", "description": "Only messages from this agent id"},
                "as": {"type": "string", "description": "Override your identity"},
            },
        },
    },
    {
        "name": "link_ack",
        "description": "Mark messages up to and including this id as read for you.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "up_to": {"type": "integer", "description": "Message id, usually the last one in your inbox"},
                "as": {"type": "string", "description": "Override your identity"},
            },
            "required": ["up_to"],
        },
    },
    {
        "name": "link_status",
        "description": "Hub health, counts and your cursor.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class Handler(BaseHTTPRequestHandler):
    server_version = "agent-link/" + VERSION
    protocol_version = "HTTP/1.1"

    # ---------- plumbing

    def log_message(self, fmt: str, *args) -> None:
        log(self.address_string(), fmt % args)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip().lower()
        if not host or host in ALLOWED_HOSTS:
            return True
        if host.endswith(".ts.net"):
            return True
        try:
            ip = ipaddress.ip_address(host)
            return ip.is_private or ip.is_loopback or ip.is_link_local
        except ValueError:
            return False

    def _authorized(self) -> bool:
        if not TOKEN:
            return True
        given = self.headers.get("Authorization", "")
        if given.startswith("Bearer "):
            given = given[7:]
        return given.strip() == TOKEN or self.headers.get("X-Agent-Link-Token", "") == TOKEN

    def _json(self, code: int, payload, extra: dict | None = None) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"ok": False, "error": message})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("body must be a JSON object")
        return data

    def _identity(self, body: dict | None = None) -> str:
        path = urllib.parse.urlsplit(self.path).path.strip("/").split("/")
        if len(path) >= 2 and path[0] == "mcp" and path[1]:
            return urllib.parse.unquote(path[1])
        header = self.headers.get("X-Agent-Id", "").strip()
        if header:
            return header
        if body:
            return str(body.get("from") or body.get("as") or "").strip()
        return ""

    # ---------- routes

    def do_GET(self) -> None:
        if not self._host_ok():
            return self._error(403, "host not allowed")
        if not self._authorized():
            return self._error(401, "token required")
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        q = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/healthz":
                return self._json(200, STORE.status())
            if path in ("/", "/index.html"):
                return self._page()
            if path in ("/install.sh", "/link.py"):
                return self._static(path)
            if path == "/v1/agents":
                ident = (q.get("as", [""])[0] or self.headers.get("X-Agent-Id", "")).strip()
                if ident:
                    STORE.touch(ident)
                return self._json(200, {"ok": True, "agents": STORE.agents()})
            if path == "/v1/messages":
                ident = (q.get("as", [""])[0] or self.headers.get("X-Agent-Id", "")).strip()
                if not ident:
                    return self._error(400, "as=<agent-id> is required")
                STORE.touch(ident)
                unread = q.get("unread", ["1"])[0] not in ("0", "false", "no")
                limit = min(max(int(q.get("limit", ["20"])[0]), 1), 200)
                since = int(q.get("since_id", ["0"])[0])
                with_id = q.get("with", [None])[0]
                msgs = STORE.inbox(ident, unread, limit, since, with_id)
                return self._json(200, {"ok": True, "as": ident, "cursor": STORE.cursor(ident),
                                        "messages": msgs})
            if path == "/v1/events":
                ident = (q.get("as", [""])[0] or self.headers.get("X-Agent-Id", "")).strip()
                if not ident:
                    return self._error(400, "as=<agent-id> is required")
                return self._events(ident, int(q.get("since_id", ["0"])[0]))
            if path == "/v1/status":
                return self._json(200, STORE.status())
            return self._error(404, "not found")
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log("GET error", repr(exc))
            return self._error(500, "internal error")

    def do_POST(self) -> None:
        if not self._host_ok():
            return self._error(403, "host not allowed")
        if not self._authorized():
            return self._error(401, "token required")
        path = urllib.parse.urlsplit(self.path).path
        try:
            body = self._body()
            if path == "/v1/messages":
                return self._send(body)
            if path == "/v1/ack":
                ident = str(body.get("as") or body.get("from") or self._identity()).strip()
                if not ident:
                    return self._error(400, "as=<agent-id> is required")
                STORE.touch(ident)
                cursor = STORE.ack(ident, body.get("up_to", 0))
                return self._json(200, {"ok": True, "as": ident, "cursor": cursor})
            if path == "/v1/register":
                ident = self._identity(body) or str(body.get("id") or "").strip()
                if not ident:
                    return self._error(400, "an identity is required")
                row = STORE.touch(ident, body.get("name", ""), body.get("machine", ""),
                                  body.get("kind", ""), body.get("description", ""))
                row["first_seen"], row["last_seen"] = iso(row["first_seen"]), iso(row["last_seen"])
                row["online"] = True
                return self._json(200, {"ok": True, "agent": row})
            if path == "/mcp" or path.startswith("/mcp/"):
                return self._mcp(body)
            return self._error(404, "not found")
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:  # pragma: no cover - defensive
            log("POST error", repr(exc))
            return self._error(500, "internal error")

    def _send(self, body: dict) -> None:
        sender = self._identity(body)
        if not sender:
            return self._error(400, "sender identity required (from/as or /mcp/<id>)")
        STORE.touch(sender)
        to = body.get("to") or body.get("recipients")
        if not to:
            return self._error(400, "'to' is required ('*' for everyone)")
        msg = STORE.send(sender, to, body.get("kind", "note"), body.get("subject", ""),
                         body.get("body") or body.get("message") or "",
                         body.get("meta"), body.get("reply_to"))
        return self._json(201, {"ok": True, "message": msg})

    # ---------- SSE

    def _events(self, ident: str, since: int) -> None:
        STORE.touch(ident)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        last = since or STORE.cursor(ident)
        try:
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                msgs = STORE.inbox(ident, False, 50, last, None)
                for m in msgs:
                    data = json.dumps(normalize(m), ensure_ascii=False)
                    self.wfile.write(f"data: {data}\n\n".encode())
                    last = m["id"]
                self.wfile.write(b": ping\n\n")
                self.wfile.flush()
                time.sleep(3)
        except (BrokenPipeError, ConnectionResetError):
            return

    # ---------- MCP

    def _mcp(self, body: dict) -> None:
        req_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params") or {}
        ident = self._identity()
        if method.startswith("notifications/"):
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if method == "initialize":
            if ident:
                STORE.touch(ident)
            return self._json(200, {
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or PROTOCOLS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "agent-link", "version": VERSION},
                    "instructions": INSTRUCTIONS,
                },
            })
        if method == "ping":
            return self._json(200, {"jsonrpc": "2.0", "id": req_id, "result": {}})
        if method == "tools/list":
            if ident:
                STORE.touch(ident)
            return self._json(200, {"jsonrpc": "2.0", "id": req_id,
                                    "result": {"tools": TOOLS}})
        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            if ident:
                STORE.touch(ident)
            try:
                text, is_error = self._call_tool(name, args, ident)
            except ValueError as exc:
                text, is_error = f"error: {exc}", True
            result = {"content": [{"type": "text", "text": text}], "isError": is_error}
            return self._json(200, {"jsonrpc": "2.0", "id": req_id, "result": result})
        return self._json(200, {"jsonrpc": "2.0", "id": req_id,
                                "error": {"code": -32601, "message": f"unknown method {method}"}})

    def _call_tool(self, name: str, args: dict, ident: str) -> tuple[str, bool]:
        who = str(args.get("from") or args.get("as") or ident).strip()
        if name == "link_whoami":
            agents = STORE.agents()
            ids = ", ".join(a["id"] for a in agents) or "(none yet)"
            me = who or "(unbound)"
            return (f"You are {me} on {PUBLIC_URL}.\nKnown identities: {ids}\n"
                    f"Your unread: {STORE._unread_after(who, STORE.cursor(who), 200) if who else 0}"
                    .strip()), False
        if name == "link_agents":
            agents = STORE.agents()
            if not agents:
                return "No agents registered yet.", False
            lines = []
            for a in agents:
                mark = "online" if a["online"] else f"last seen {a['last_seen']}"
                lines.append(f"{a['id']:<18} {a['kind']:<6} {a['machine']:<9} {mark:<26} "
                             f"unread={a['unread']}  {a['name']}")
            return "\n".join(lines), False
        if name == "link_send":
            if not who:
                raise ValueError("sender identity required; open /mcp/<your-id> or pass from")
            if not args.get("to"):
                raise ValueError("'to' is required ('*' for everyone)")
            STORE.touch(who)
            msg = STORE.send(who, args["to"], args.get("kind", "note"), args.get("subject", ""),
                             args.get("message") or args.get("body") or "", args.get("meta"),
                             args.get("reply_to"))
            return (f"sent [{msg['id']}] {who} → {','.join(msg['recipients'])} "
                    f"({msg['kind']})"), False
        if name == "link_inbox":
            if not who:
                raise ValueError("identity required; open /mcp/<your-id> or pass as")
            STORE.touch(who)
            msgs = STORE.inbox(who, bool(args.get("unread_only", True)),
                               min(int(args.get("limit", 20)), 200), int(args.get("since_id", 0)),
                               args.get("with"))
            if not msgs:
                return "Inbox empty.", False
            return "\n\n".join(message_text(m) for m in msgs), False
        if name == "link_ack":
            if not who:
                raise ValueError("identity required; open /mcp/<your-id> or pass as")
            STORE.touch(who)
            cursor = STORE.ack(who, args.get("up_to", 0))
            return f"{who} read up to message {cursor}.", False
        if name == "link_status":
            st = STORE.status()
            st["as"] = who or "(unbound)"
            st["cursor"] = STORE.cursor(who) if who else 0
            return json.dumps(st, indent=2), False
        raise ValueError(f"unknown tool {name}")

    # ---------- static

    def _static(self, name: str) -> None:
        file = STATIC_DIR / name.lstrip("/")
        try:
            text = file.read_text()
        except OSError:
            return self._error(404, "not found")
        if name == "/install.sh":
            text = text.replace("__HUB_URL__", PUBLIC_URL)
        data = text.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- page

    def _page(self) -> None:
        agents, msgs = STORE.agents(), STORE.recent(25)
        rows_a = "".join(
            f"<tr><td><span class='dot {'on' if a['online'] else 'off'}'></span>{esc(a['id'])}</td>"
            f"<td>{esc(a['kind'])}</td><td>{esc(a['machine'])}</td><td>{esc(a['name'])}</td>"
            f"<td class='n'>{a['unread']}</td><td class='m'>{esc(a['last_seen'])}</td></tr>"
            for a in agents
        )
        rows_m = "".join(
            f"<tr><td class='m'>{esc(m['ts'])}</td><td>{esc(m['from'])}</td>"
            f"<td>{esc(','.join(m['recipients']))}</td><td><b>{esc(m['kind'])}</b></td>"
            f"<td>{esc(m['subject'])}</td><td>{esc(' '.join(m['body'].split())[:160])}</td></tr>"
            for m in reversed(msgs)
        )
        html = f"""<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-link</title>
<style>
:root{{--bg:#0e1116;--panel:#161b22;--line:#252c36;--fg:#e6edf3;--dim:#8b949e;--ok:#3fb950;--accent:#58a6ff}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,-apple-system,Segoe UI,Roboto,sans-serif}}
main{{max-width:1180px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:22px;margin:0 0 4px;letter-spacing:-.01em}}
p.sub{{color:var(--dim);margin:0 0 26px}}
code{{background:var(--panel);border:1px solid var(--line);border-radius:5px;padding:1px 6px;font-size:12.5px}}
h2{{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--dim);margin:30px 0 10px;font-weight:600}}
table{{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th{{text-align:left;font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--dim);padding:9px 12px;border-bottom:1px solid var(--line);font-weight:600}}
td{{padding:9px 12px;border-bottom:1px solid var(--line);vertical-align:top}}
tr:last-child td{{border-bottom:0}}
td.n{{text-align:right;font-variant-numeric:tabular-nums}}
td.m{{color:var(--dim);white-space:nowrap;font-size:12.5px}}
.dot{{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:8px;background:#484f58}}
.dot.on{{background:var(--ok);box-shadow:0 0 0 3px rgba(63,185,80,.15)}}
.empty{{color:var(--dim);padding:24px 0}}
.faces{{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:22px 0 0}}
.face{{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.face b{{color:var(--accent);font-size:12px;letter-spacing:.04em;text-transform:uppercase}}
.face div{{color:var(--dim);font-size:12.5px;margin-top:4px}}
</style>
<main>
<h1>agent-link</h1>
<p class="sub">One bus between the Mac, the Fedora laptop and the agents on both. Store-and-forward, so an offline machine loses nothing.</p>
<div class="faces">
<div class="face"><b>MCP</b><div><code>{esc(PUBLIC_URL)}/mcp/&lt;agent-id&gt;</code></div></div>
<div class="face"><b>HTTP API</b><div><code>GET /v1/messages</code> <code>POST /v1/messages</code></div></div>
<div class="face"><b>CLI</b><div><code>agent-link inbox</code></div></div>
</div>
<h2>Agents</h2>
{'<table><tr><th>id</th><th>kind</th><th>machine</th><th>name</th><th>unread</th><th>last seen</th></tr>' + rows_a + '</table>' if agents else '<div class="empty">No agents yet.</div>'}
<h2>Recent messages</h2>
{'<table><tr><th>when</th><th>from</th><th>to</th><th>kind</th><th>subject</th><th>body</th></tr>' + rows_m + '</table>' if msgs else '<div class="empty">Nothing sent yet.</div>'}
</main></html>"""
        data = html.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def esc(value) -> str:
    return (str(value or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def main() -> None:
    global STORE
    STORE = Store(DB_PATH)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    log(f"agent-link {VERSION} on :{PORT} db={DB_PATH} public={PUBLIC_URL}"
        f"{' token required' if TOKEN else ''}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
