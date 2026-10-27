#!/usr/bin/env python3
"""agent-link CLI: talk to other machines and agents on the tailnet from any shell.

  agent-link init --id mac-opencode --name "OpenCode on the Mac"
  agent-link agents
  agent-link send mac-hermes "Please rerun the ledger build" -k task -s "ledger"
  agent-link inbox            # unread, oldest first
  agent-link read 42          # acknowledge up to id 42
  agent-link watch --notify   # follow new messages in a terminal
  agent-link mcp              # stdio MCP server for agents without remote MCP

The hub URL and identity live in ~/.config/agent-link/config.json.
Environment overrides: AGENT_LINK_URL, AGENT_LINK_ID.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_URL = "http://ubuntu03:30110"
CONFIG = Path(os.environ.get("AGENT_LINK_CONFIG", Path.home() / ".config/agent-link/config.json"))
VERSION = "1.0.0"
TIMEOUT = 15


def config() -> dict:
    try:
        return json.loads(CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def save_config(cfg: dict) -> None:
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    os.replace(tmp, CONFIG)


def hub_url() -> str:
    return (os.environ.get("AGENT_LINK_URL") or config().get("url") or DEFAULT_URL).rstrip("/")


def default_id() -> str:
    machine = (config().get("machine") or socket.gethostname().split(".")[0]).lower()
    return f"{machine}-{os.environ.get('USER', 'agent')}"


def identity(args) -> str:
    return (getattr(args, "as_id", None) or os.environ.get("AGENT_LINK_ID")
            or config().get("id") or default_id())


def call(method: str, path: str, payload: dict | None = None, token: str | None = None,
         timeout: float = TIMEOUT, ident: str | None = None):
    url = hub_url() + path
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    tok = token or os.environ.get("AGENT_LINK_TOKEN") or config().get("token")
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if ident:
        headers["X-Agent-Id"] = ident
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        try:
            err = json.loads(raw).get("error")
        except json.JSONDecodeError:
            err = raw.strip() or exc.reason
        raise SystemExit(f"agent-link: {exc.code} {err}  ({url})") from None
    except urllib.error.URLError as exc:
        raise SystemExit(f"agent-link: cannot reach {url}: {exc.reason}") from None


def tty() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if tty() else text


def rel(ts: str) -> str:
    try:
        then = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except ValueError:
        return ts
    delta = time.time() - then
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


# ---------------------------------------------------------------- commands

def cmd_init(args) -> int:
    cfg = config()
    cfg["url"] = (args.url or cfg.get("url") or DEFAULT_URL).rstrip("/")
    cfg["id"] = args.id or cfg.get("id") or default_id()
    cfg["machine"] = args.machine or cfg.get("machine") or socket.gethostname().split(".")[0]
    if args.name:
        cfg["name"] = args.name
    cfg.setdefault("name", cfg["id"])
    save_config(cfg)
    row = call("POST", "/v1/register", {
        "name": cfg["name"], "machine": cfg["machine"],
        "kind": args.kind or ("human" if cfg["id"].endswith("simon") else "agent"),
        "description": args.description or "",
    }, token=cfg.get("token"), ident=cfg["id"])
    print(f"{CONFIG}  id={cfg['id']}  hub={cfg['url']}  kind={row['agent']['kind']}")
    return 0


def cmd_id(args) -> int:
    cfg = config()
    print(f"id      {identity(args)}")
    print(f"hub     {hub_url()}")
    print(f"name    {cfg.get('name', '-')}")
    print(f"machine {cfg.get('machine', socket.gethostname())}")
    return 0


def cmd_agents(args) -> int:
    data = call("GET", "/v1/agents?as=" + urllib.parse.quote(identity(args)))
    agents = data["agents"]
    if args.json:
        print(json.dumps(agents, indent=2))
        return 0
    if not agents:
        print("No agents registered yet.")
        return 0
    print(paint("2", f"{'ID':<18} {'KIND':<6} {'MACHINE':<10} {'STATE':<12} {'UNREAD':>6}  NAME"))
    for a in agents:
        state = paint("32", "online") if a["online"] else paint("2", rel(a["last_seen"]))
        unread = paint("33;1", str(a["unread"])) if a["unread"] else "0"
        print(f"{a['id']:<18} {a['kind']:<6} {a['machine']:<10} {state:<12} "
              f"{unread:>6}  {a['name']}")
    return 0


def cmd_send(args) -> int:
    body = " ".join(args.message) if args.message else ""
    if args.file:
        body = Path(args.file).expanduser().read_text()
    elif not body and not sys.stdin.isatty():
        body = sys.stdin.read()
    if not body.strip():
        raise SystemExit("agent-link send: nothing to send (give a message, --file or stdin)")
    payload = {"to": args.to, "body": body, "kind": args.kind, "subject": args.subject,
               "message": body}
    if args.reply:
        payload["reply_to"] = args.reply
    if args.meta:
        try:
            payload["meta"] = json.loads(args.meta)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"agent-link send: --meta is not JSON: {exc}") from None
    res = call("POST", "/v1/messages", payload, timeout=TIMEOUT, ident=identity(args))
    m = res["message"]
    if not args.json:
        print(f"sent [{m['id']}] → {','.join(m['recipients'])} ({m['kind']})")
    else:
        print(json.dumps(m, indent=2))
    return 0


def cmd_inbox(args) -> int:
    ident = identity(args)
    query = {"as": ident, "limit": str(args.limit), "unread": "0" if args.all else "1"}
    if args.since:
        query["since_id"] = str(args.since)
    if args.with_id:
        query["with"] = args.with_id
    data = call("GET", "/v1/messages?" + urllib.parse.urlencode(query))
    msgs = data["messages"]
    if args.json:
        print(json.dumps(msgs, indent=2))
        return 0
    if not msgs:
        print(paint("2", "Inbox empty." if not args.all else "No messages yet."))
        return 0
    for m in msgs:
        head = (f"{paint('1;36', '[' + str(m['id']) + ']')} "
                f"{paint('1', m['from'])} → {','.join(m['recipients'])}  "
                f"{paint('2', rel(m['ts']))}")
        kind = "" if m["kind"] == "note" else paint("35", f"  {m['kind']}")
        subj = f"\n  {paint('1', m['subject'])}" if m["subject"] else ""
        body = "\n".join("  " + line for line in m["body"].splitlines() or [""])
        print(head + kind + subj)
        print(body)
        if m.get("meta"):
            print(paint("2", "  meta " + json.dumps(m["meta"], ensure_ascii=False)))
        print()
    if not args.all:
        print(paint("2", f"{len(msgs)} unread · acknowledge with: agent-link read {msgs[-1]['id']}"))
    return 0


def cmd_read(args) -> int:
    ident = identity(args)
    up_to = args.up_to
    if up_to is None:
        data = call("GET", f"/v1/messages?as={urllib.parse.quote(ident)}&limit=1&unread=1")
        if not data["messages"]:
            print("Nothing unread.")
            return 0
        up_to = data["messages"][-1]["id"]
    res = call("POST", "/v1/ack", {"as": ident, "up_to": int(up_to)}, ident=ident)
    print(f"{ident} read up to {res['cursor']}.")
    return 0


def cmd_status(args) -> int:
    ident = identity(args)
    st = call("GET", "/v1/status")
    me = next((a for a in call("GET", "/v1/agents?as=" + urllib.parse.quote(ident))["agents"]
               if a["id"] == ident), {})
    st.update({"as": ident, "cursor": me.get("cursor", 0), "unread": me.get("unread", 0)})
    if args.json:
        print(json.dumps(st, indent=2))
        return 0
    print(f"{st['service']} {st['version']}  {hub_url()}")
    print(f"up since {st['up_since']}  agents {st['agents']}  messages {st['messages']}")
    print(f"you {ident}  unread {st['unread']}  cursor {st['cursor']}")
    return 0


def notify(title: str, text: str) -> None:
    try:
        if platform.system() == "Darwin":
            script = ('display notification ' + json.dumps(text) +
                      ' with title ' + json.dumps(title))
            subprocess.run(["osascript", "-e", script], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", title, text], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def cmd_watch(args) -> int:
    ident = identity(args)
    last = args.since or 0
    print(paint("2", f"watching as {ident} on {hub_url()} · ctrl-c to stop"))
    try:
        while True:
            data = call("GET", "/v1/messages?" + urllib.parse.urlencode(
                {"as": ident, "unread": "0", "since_id": str(last), "limit": "50"}))
            if data["messages"]:
                for m in data["messages"]:
                    last = max(last, m["id"])
                    subj = f" · {m['subject']}" if m["subject"] else ""
                    label = f"{m['from']} [{m['kind']}]{subj}"
                    print(f"\n{paint('1;36', '[' + str(m['id']) + ']')} {label}\n"
                          + "\n".join("  " + line for line in m["body"].splitlines()))
                    if args.notify:
                        notify(f"agent-link · {m['from']}", f"[{m['kind']}] {subj or m['body'][:120]}")
            elif last == 0:
                last = data["cursor"]
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print()
        return 0


def cmd_mcp(args) -> int:
    """stdio MCP server: forward JSON-RPC lines to the hub at /mcp/<id>."""
    ident = identity(args)
    url = f"{hub_url()}/mcp/{urllib.parse.quote(ident)}"
    token = os.environ.get("AGENT_LINK_TOKEN") or config().get("token")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        payload = json.dumps(req).encode()
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(url, data=payload, headers=headers, method="POST"),
                    timeout=60) as resp:
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode(errors="replace")
        except urllib.error.URLError:
            raw = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                              "error": {"code": -32000, "message": "hub unreachable"}})
        if raw.strip():
            sys.stdout.write(raw + "\n")
            sys.stdout.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-link", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"agent-link {VERSION}")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--as", dest="as_id", metavar="ID", help="act as this identity")

    sp = sub.add_parser("init", help="write the config and register this machine")
    sp.add_argument("--id", help="identity, e.g. mac-opencode or fedora-opencode")
    sp.add_argument("--name", help="human name shown to others")
    sp.add_argument("--machine", help="machine label (mac, home, ubuntu03…)")
    sp.add_argument("--url", help=f"hub URL (default {DEFAULT_URL})")
    sp.add_argument("--kind", choices=["agent", "human", "machine"])
    sp.add_argument("--description", help="what this agent is for")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("id", aliases=["whoami"], help="show this identity and hub")
    common(sp)
    sp.set_defaults(func=cmd_id)

    sp = sub.add_parser("agents", help="who is on the bus")
    sp.add_argument("--json", action="store_true")
    common(sp)
    sp.set_defaults(func=cmd_agents)

    sp = sub.add_parser("send", help="send a message")
    sp.add_argument("to", help="recipient id or '*' for everyone")
    sp.add_argument("message", nargs="*", help="the message text")
    sp.add_argument("-k", "--kind", default="note",
                    choices=["note", "task", "question", "answer", "handoff", "result", "status"])
    sp.add_argument("-s", "--subject", default="")
    sp.add_argument("--reply", type=int, help="id of the message being answered")
    sp.add_argument("--meta", help="JSON object with structured payload")
    sp.add_argument("--file", help="read the body from this file")
    sp.add_argument("--json", action="store_true")
    common(sp)
    sp.set_defaults(func=cmd_send)

    sp = sub.add_parser("inbox", help="read messages sent to you")
    sp.add_argument("-a", "--all", action="store_true", help="include already-read messages")
    sp.add_argument("-n", "--limit", type=int, default=20)
    sp.add_argument("--since", type=int, default=0)
    sp.add_argument("--with", dest="with_id", metavar="ID", help="only from this agent")
    sp.add_argument("--json", action="store_true")
    common(sp)
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("read", help="acknowledge messages up to an id")
    sp.add_argument("up_to", nargs="?", type=int)
    common(sp)
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("status", help="hub health and your cursor")
    sp.add_argument("--json", action="store_true")
    common(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("watch", help="follow new messages in a terminal")
    sp.add_argument("-i", "--interval", type=float, default=5)
    sp.add_argument("--since", type=int, default=0)
    sp.add_argument("--notify", action="store_true", help="also raise a desktop notification")
    common(sp)
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("mcp", help="stdio MCP server (for clients without remote MCP)")
    common(sp)
    sp.set_defaults(func=cmd_mcp)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
