"""Minimal MCP (Streamable HTTP) client for the Blaze server, taken from the rules bot."""
import http.client, json, ssl, sys, time, urllib.parse

VERSION = "royale-1"


class MCPError(Exception):
    pass


class MCPClient:
    """Minimal MCP client for the Streamable HTTP transport (JSON or SSE responses)."""

    def __init__(self, url, token=None, timeout=15):
        self.url = urllib.parse.urlparse(url)
        self.token = token
        self.timeout = timeout
        self.session_id = None
        self.next_id = 1
        self.conn = None

    def _connect(self):
        host = self.url.hostname
        port = self.url.port
        if self.url.scheme == "https":
            self.conn = http.client.HTTPSConnection(host, port or 443, timeout=self.timeout,
                                                    context=ssl.create_default_context())
        else:
            self.conn = http.client.HTTPConnection(host, port or 80, timeout=self.timeout)

    def _post(self, payload, expect_reply=True):
        body = json.dumps(payload).encode()
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream",
                   "MCP-Protocol-Version": "2025-11-25"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        path = self.url.path or "/"
        if self.url.query:
            path += "?" + self.url.query
        for attempt in range(3):
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request("POST", path, body=body, headers=headers)
                resp = self.conn.getresponse()
                raw = resp.read()
                break
            except (http.client.HTTPException, OSError):
                self.conn = None
                if attempt == 2:
                    raise
                time.sleep(0.2)
        sid = resp.getheader("Mcp-Session-Id")
        if sid:
            self.session_id = sid
        if resp.status in (401, 403):
            raise MCPError(f"HTTP {resp.status}: the server wants authorization. "
                           "Pass a token with --token (see README).")
        if resp.status == 404 and self.session_id and payload.get("method") != "initialize":
            raise MCPError("session expired")
        if resp.status >= 400:
            raise MCPError(f"HTTP {resp.status}: {raw[:300]!r}")
        if not expect_reply:
            return None
        ctype = (resp.getheader("Content-Type") or "").lower()
        text = raw.decode("utf-8", "replace")
        if "text/event-stream" in ctype:
            msg = None
            for block in text.split("\n\n"):
                data = "\n".join(l[5:].lstrip() for l in block.splitlines() if l.startswith("data:"))
                if not data:
                    continue
                try:
                    m = json.loads(data)
                except ValueError:
                    continue
                if m.get("id") == payload.get("id"):
                    msg = m
            if msg is None:
                raise MCPError("no reply in event stream")
        else:
            msg = json.loads(text) if text.strip() else {}
        if "error" in msg:
            raise MCPError(str(msg["error"]))
        return msg.get("result")

    def initialize(self):
        self.session_id = None
        res = self._post({"jsonrpc": "2.0", "id": self._id(), "method": "initialize",
                          "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                     "clientInfo": {"name": "blaze-bot", "version": VERSION}}})
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, expect_reply=False)
        return res

    def _id(self):
        self.next_id += 1
        return self.next_id

    def list_tools(self):
        res = self._post({"jsonrpc": "2.0", "id": self._id(), "method": "tools/list", "params": {}})
        return [t["name"] for t in res.get("tools", [])]

    def call(self, tool, args):
        payload = {"jsonrpc": "2.0", "id": self._id(), "method": "tools/call",
                   "params": {"name": tool, "arguments": args}}
        try:
            res = self._post(payload)
        except MCPError as e:
            if "session expired" not in str(e):
                raise
            self.initialize()
            payload["id"] = self._id()
            res = self._post(payload)
        if res is None:
            raise MCPError("empty result")
        if res.get("structuredContent"):
            return res["structuredContent"]
        texts = [c.get("text", "") for c in res.get("content", []) if c.get("type") == "text"]
        joined = "".join(texts)
        if res.get("isError"):
            raise MCPError("tool error: " + joined[:300])
        try:
            return json.loads(joined)
        except ValueError:
            return {"_text": joined}


CANDIDATE_URLS = ["https://blaze.nine10go.com/mcp", "https://blaze.nine10go.com/api/mcp",
                  "https://blaze.nine10go.com/mcp/"]


def find_url(token):
    for u in CANDIDATE_URLS:
        try:
            c = MCPClient(u, token, timeout=8)
            c.initialize()
            if {"observe", "move", "game_action"} <= set(c.list_tools()):
                print("found the Blaze server at", u)
                return u
        except MCPError as e:
            if "authorization" in str(e):
                sys.exit(f"{u} needs authorization: {e}")
        except Exception:
            pass
    return None
