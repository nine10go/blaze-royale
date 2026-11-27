#!/usr/bin/env python3
"""A stand-in for the Meta Model API, for testing without a key or credit.

    python3 mock_ember.py --port 8700 &
    export EMBER_BASE_URL=http://127.0.0.1:8700/v1 MODEL_API_KEY=mock ROYALE_LEDGER=/tmp/mock-spend.json

Answers /v1/models and /v1/chat/completions with random but valid intents and plausible
token counts, after a random delay, so the whole tournament can be tested offline.
"""
import argparse, json, random, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

INTENTS = ["exit", "gems", "drops", "explore", "hunt", "safe", "shadow"]
LINES = ["Watch me work.", "Too slow!", "That gem was mine.", "Nobody saw that.", "Exit is close, I can smell it.",
         "Running is a strategy.", "Come and get me.", "Patience pays."]
CALLS = ["What a dash by {a}! The crowd is on its feet.", "{a} and {b} trade blows in the alleys!",
         "{a} is quietly building a lead while everyone else panics.", "Ouch, that trail caught {b} clean."]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.endswith("/models"):
            return self._send(200, {"data": [{"id": "muse-ember-1.0"}, {"id": "muse-spark-1.3"}]})
        self._send(404, {"error": "no"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        if self.headers.get("Authorization", "") == "":
            return self._send(401, {"error": {"message": "missing key"}})
        msgs = req.get("messages") or []
        text_in = " ".join(m.get("content", "") for m in msgs)
        time.sleep(random.uniform(*self.server.lat))
        if "commentator" in text_in.lower():
            names = [w for w in ["Hunter", "Coward", "Hoarder", "Speedrunner", "Explorer", "Trickster",
                                 "Tank", "Gambler", "Shadow", "Diplomat"] if w in text_in] or ["Hunter", "Coward"]
            a, b = random.choice(names), random.choice(names)
            out = random.choice(CALLS).format(a=a, b=b)
        else:
            out = json.dumps({"intent": random.choice(INTENTS), "say": random.choice(LINES)})
        pin = len(text_in) // 4 + 20
        reasoning = random.randint(0, 60)
        pout = len(out) // 4 + 5 + reasoning
        self._send(200, {"id": "mock", "model": req.get("model"),
                         "choices": [{"index": 0, "message": {"role": "assistant", "content": out},
                                      "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": pin, "completion_tokens": pout, "total_tokens": pin + pout,
                                   "completion_tokens_details": {"reasoning_tokens": reasoning}}})


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8700)
    ap.add_argument("--lat", type=float, nargs=2, default=[0.3, 1.2])
    a = ap.parse_args()
    s = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    s.lat = a.lat
    print("mock Ember on http://127.0.0.1:%d/v1" % a.port, flush=True)
    s.serve_forever()
