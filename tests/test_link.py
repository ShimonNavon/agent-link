#!/usr/bin/env python3
"""Smoke tests for the agent-link hub and CLI. No network beyond localhost."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PORT = 18099
BASE = f"http://127.0.0.1:{PORT}"


def http(method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


class HubTest(unittest.TestCase):
    proc = None
    tmp = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        env = {**os.environ, "PORT": str(PORT), "AGENT_LINK_DB": f"{cls.tmp.name}/link.db",
               "PUBLIC_URL": BASE, "PYTHONUNBUFFERED": "1"}
        cls.proc = subprocess.Popen([sys.executable, str(HERE / "hub.py")], env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                http("GET", "/healthz")
                return
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("hub did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=5)
        cls.tmp.cleanup()

    def test_health(self):
        st = http("GET", "/healthz")
        self.assertTrue(st["ok"])
        self.assertEqual(st["service"], "agent-link")

    def test_send_and_inbox(self):
        sent = http("POST", "/v1/messages", {"from": "t1-mac", "to": "t1-fedora",
                                             "kind": "task", "subject": "build",
                                             "body": "run the tests"})["message"]
        self.assertEqual(sent["from"], "t1-mac")
        inbox = http("GET", "/v1/messages?as=t1-fedora&unread=1&with=t1-mac")
        self.assertEqual([m["id"] for m in inbox["messages"]], [sent["id"]])
        ack = http("POST", "/v1/ack", {"as": "t1-fedora", "up_to": sent["id"]})
        self.assertEqual(ack["cursor"], sent["id"])
        again = http("GET", "/v1/messages?as=t1-fedora&unread=1&with=t1-mac")
        self.assertEqual(again["messages"], [])

    def test_broadcast_and_agents(self):
        http("POST", "/v1/messages", {"from": "t2-hermes", "to": "*", "body": "hello all"})
        inbox = http("GET", "/v1/messages?as=t2-opencode&unread=1")
        self.assertTrue(any(m["from"] == "t2-hermes" for m in inbox["messages"]))
        agents = {a["id"] for a in http("GET", "/v1/agents")["agents"]}
        self.assertIn("t2-hermes", agents)
        self.assertIn("t2-opencode", agents)

    def test_mcp_identity_and_tools(self):
        init = http("POST", "/mcp/t3-claude", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                                "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init["result"]["serverInfo"]["name"], "agent-link")
        tools = http("POST", "/mcp/t3-claude", {"jsonrpc": "2.0", "id": 2,
                                                 "method": "tools/list"})["result"]["tools"]
        self.assertEqual(len(tools), 6)
        sent = http("POST", "/mcp/t3-claude", {
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "link_send", "arguments": {"to": "t3-hermes", "message": "ping"}}})
        self.assertFalse(sent["result"]["isError"])
        inbox = http("POST", "/mcp/t3-hermes", {
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "link_inbox", "arguments": {}}})
        self.assertIn("t3-claude", inbox["result"]["content"][0]["text"])
        who = http("POST", "/mcp/t3-fedora", {
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "link_whoami", "arguments": {}}})
        self.assertIn("You are t3-fedora", who["result"]["content"][0]["text"])

    def test_cli_roundtrip(self):
        env = {**os.environ, "AGENT_LINK_URL": BASE, "AGENT_LINK_CONFIG": f"{self.tmp.name}/cfg.json",
               "AGENT_LINK_ID": "t4-simon", "NO_COLOR": "1"}
        run = lambda *a: subprocess.run([sys.executable, str(HERE / "link.py"), *a], env=env,
                                        capture_output=True, text=True, timeout=20)
        self.assertEqual(run("init", "--id", "t4-simon", "--kind", "human").returncode, 0)
        self.assertEqual(run("send", "t4-opencode", "from the fedora laptop").returncode, 0)
        out = run("inbox", "--all", "--as", "t4-opencode").stdout
        self.assertIn("from the fedora laptop", out)
        self.assertIn("t4-opencode", run("agents").stdout)
        self.assertIn("t4-simon", run("status").stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
