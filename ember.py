"""Muse Ember client (Meta Model API, OpenAI-compatible) with a hard spending cap.

- The key is read from the MODEL_API_KEY environment variable only. It is never written
  to a file or a log.
- Every call is priced from the token counts the API returns. Before a call is sent, its
  worst case (prompt estimate + max_tokens of output) is reserved against the cap; if the
  reservation would cross the cap, the call is refused and nothing is sent.
- Spend is kept in a ledger file (spend.json) shared by every bot, commentator and
  tournament in this folder, so the cap holds across runs and processes.

Python 3.9+ standard library only.
"""
import http.client, json, os, ssl, threading, time, urllib.parse

try:
    import fcntl  # Linux / macOS
except ImportError:  # pragma: no cover
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE = "https://api.meta.ai/v1"
DEFAULT_MODEL = "muse-ember-1.0"
# US dollars per 1M tokens, from dev.meta.ai/models/muse-ember (Nov 26, 2026), under 200k context.
PRICE = {"input": 0.10, "cached": 0.004, "output": 0.50}
DEFAULT_CAP = float(os.environ.get("ROYALE_SPEND_CAP", "6.00"))   # well under the $10 credit
HARD_CEILING = 9.00   # no flag or env var can raise the cap above this
LEDGER = os.environ.get("ROYALE_LEDGER") or os.path.join(HERE, "spend.json")


class EmberError(Exception):
    pass


class CapReached(EmberError):
    pass


def cost_of(usage):
    """Dollars for one response's usage block."""
    pin = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    pout = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    det = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cached = int(det.get("cached_tokens") or 0)
    fresh = max(0, pin - cached)
    return (fresh * PRICE["input"] + cached * PRICE["cached"] + pout * PRICE["output"]) / 1e6


class Ledger:
    """Process- and thread-safe spend ledger with reservations."""

    def __init__(self, path=LEDGER, cap=DEFAULT_CAP):
        self.path = path
        self.cap = min(float(cap), HARD_CEILING)
        self.lock = threading.Lock()
        if not os.path.exists(path):
            self._write({"spent": 0.0, "reserved": 0.0, "calls": 0, "in": 0, "out": 0,
                         "by_role": {}, "started": time.time()})

    def _read(self):
        try:
            with open(self.path) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {"spent": 0.0, "reserved": 0.0, "calls": 0, "in": 0, "out": 0, "by_role": {}}

    def _write(self, d):
        tmp = self.path + ".tmp%d" % os.getpid()
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, self.path)

    def _locked(self, fn):
        with self.lock:
            lf = open(self.path + ".lock", "a+")
            try:
                if fcntl:
                    fcntl.flock(lf, fcntl.LOCK_EX)
                d = self._read()
                r = fn(d)
                self._write(d)
                return r
            finally:
                if fcntl:
                    fcntl.flock(lf, fcntl.LOCK_UN)
                lf.close()

    def reserve(self, amount):
        def f(d):
            if d["spent"] + d.get("reserved", 0) + amount > self.cap:
                return False
            d["reserved"] = d.get("reserved", 0) + amount
            return True
        return self._locked(f)

    def settle(self, reserved, actual, usage, role):
        def f(d):
            d["reserved"] = max(0.0, d.get("reserved", 0) - reserved)
            d["spent"] = d["spent"] + actual
            d["calls"] = d.get("calls", 0) + 1
            d["in"] = d.get("in", 0) + int(usage.get("prompt_tokens") or 0)
            d["out"] = d.get("out", 0) + int(usage.get("completion_tokens") or 0)
            br = d.setdefault("by_role", {}).setdefault(role, {"calls": 0, "spent": 0.0})
            br["calls"] += 1
            br["spent"] += actual
        self._locked(f)

    def release(self, reserved):
        def f(d):
            d["reserved"] = max(0.0, d.get("reserved", 0) - reserved)
        self._locked(f)

    def snapshot(self):
        d = self._read()
        d["cap"] = self.cap
        return d


class Ember:
    """Chat completions over a kept-alive HTTPS connection. One instance per thread."""

    def __init__(self, ledger, model=None, base_url=None, effort=None, max_tokens=300,
                 api_key=None, timeout=8.0):
        self.key = api_key or os.environ.get("MODEL_API_KEY")
        if not self.key:
            raise EmberError("MODEL_API_KEY is not set. Run: export MODEL_API_KEY=<your Meta Model API key>")
        self.ledger = ledger
        self.model = model or os.environ.get("EMBER_MODEL", DEFAULT_MODEL)
        self.base = urllib.parse.urlparse((base_url or os.environ.get("EMBER_BASE_URL", DEFAULT_BASE)).rstrip("/"))
        self.effort = effort if effort is not None else os.environ.get("EMBER_EFFORT", "minimal")
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.conn = None

    def _connect(self):
        host, port = self.base.hostname, self.base.port
        if self.base.scheme == "http":
            self.conn = http.client.HTTPConnection(host, port or 80, timeout=self.timeout)
            return
        ctx = ssl.create_default_context()
        proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
        if proxy:
            p = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
            self.conn = http.client.HTTPSConnection(p.hostname, p.port or 8080, timeout=self.timeout, context=ctx)
            self.conn.set_tunnel(host, port or 443)
        else:
            self.conn = http.client.HTTPSConnection(host, port or 443, timeout=self.timeout, context=ctx)

    def _request(self, method, path, body):
        headers = {"Authorization": "Bearer " + self.key, "Content-Type": "application/json",
                   "User-Agent": "blaze-royale/1"}
        last = None
        for _ in range(2):
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request(method, (self.base.path or "") + path, body=body, headers=headers)
                resp = self.conn.getresponse()
                return resp.status, resp.read(), resp
            except (OSError, http.client.HTTPException) as e:
                last = e
                self.conn = None
                if "timed out" in str(e):
                    raise EmberError("timeout")
        raise EmberError("connection failed: %s" % last)

    def list_models(self):
        status, raw, _ = self._request("GET", "/models", None)
        if status != 200:
            raise EmberError("HTTP %d: %s" % (status, raw[:300].decode("utf-8", "replace")))
        return [m.get("id") for m in json.loads(raw).get("data", [])]

    def chat(self, messages, role="bot", max_tokens=None, json_mode=False):
        """Returns (text, info). info has latency_s, usage, cost, reasoning_tokens."""
        mt = max_tokens or self.max_tokens
        est_in = sum(len(m.get("content", "")) for m in messages) / 3.2 + 50
        worst = (est_in * PRICE["input"] + mt * PRICE["output"]) / 1e6 * 1.2
        if not self.ledger.reserve(worst):
            raise CapReached("spending cap of $%.2f reached" % self.ledger.cap)
        payload = {"model": self.model, "messages": messages, "max_completion_tokens": mt}
        if self.effort and self.effort != "default":
            payload["reasoning_effort"] = self.effort
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        t0 = time.time()
        try:
            status, raw, resp = self._request("POST", "/chat/completions", json.dumps(payload).encode())
            if status == 400 and "reasoning_effort" in payload and b"reasoning" in raw:
                # this model rejects that effort level: step up once, then drop the field
                order = ["none", "minimal", "low"]
                nxt = order[order.index(self.effort) + 1] if self.effort in order[:-1] else None
                self.effort = nxt
                if nxt:
                    payload["reasoning_effort"] = nxt
                else:
                    payload.pop("reasoning_effort")
                status, raw, resp = self._request("POST", "/chat/completions", json.dumps(payload).encode())
            if status == 400 and json_mode and b"response_format" in raw:
                payload.pop("response_format")
                status, raw, resp = self._request("POST", "/chat/completions", json.dumps(payload).encode())
        except EmberError:
            self.ledger.release(worst)
            raise
        lat = time.time() - t0
        if status != 200:
            self.ledger.release(worst)
            raise EmberError("HTTP %d: %s" % (status, raw[:400].decode("utf-8", "replace")))
        d = json.loads(raw)
        usage = d.get("usage") or {}
        if not usage:
            # no usage block: charge the worst case so the cap still holds
            actual = worst
        else:
            actual = cost_of(usage)
        self.ledger.settle(worst, actual, usage, role)
        msg = (d.get("choices") or [{}])[0].get("message") or {}
        text = msg.get("content") or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") for p in text if isinstance(p, dict))
        rdet = usage.get("completion_tokens_details") or {}
        return text, {"latency_s": round(lat, 3), "usage": usage, "cost": actual,
                      "reasoning_tokens": rdet.get("reasoning_tokens"), "effort": self.effort,
                      "rl_remaining_requests": resp.getheader("x-ratelimit-remaining-requests"),
                      "rl_remaining_tokens": resp.getheader("x-ratelimit-remaining-tokens")}


def extract_json(text):
    """First {...} object in a reply, tolerant of code fences and prose."""
    if not text:
        return None
    s = text.find("{")
    while s >= 0:
        depth = 0
        for i in range(s, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[s:i + 1])
                    except ValueError:
                        break
        s = text.find("{", s + 1)
    return None
