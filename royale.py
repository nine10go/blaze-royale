#!/usr/bin/env python3
"""Blaze Battle Royale: a tournament of Muse Ember bots with a live leaderboard and commentator.

Typical run on your machine (see README.md):
    export MODEL_API_KEY=...                       # Meta Model API key, never written to a file
    python3 royale.py --server-bin ~/blaze/server/target/release/blaze-server --blaze-root ~/blaze \\
                      --bots 36 --minutes 6 --cap 3
    open http://127.0.0.1:8099/                    # leaderboard

Format: round 1 puts every bot into one of the arenas at once (6 per arena, the game's
limit for agents). The best of each arena go through to a 6-bot final. Within a heat, bots are ranked
by level, then score; being eliminated earlier ranks lower among equals.

Without --server-bin, pass --arena <MCP URL> once per running Blaze server instead
(those worlds are then not reset between heats).
"""
import argparse, collections, json, os, signal, subprocess, sys, threading, time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from functools import partial

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bot import Bot  # noqa: E402
from ember import CapReached, DEFAULT_CAP, Ember, EmberError, HARD_CEILING, Ledger  # noqa: E402
from personas import PERSONAS, roster  # noqa: E402

WEB = os.path.join(HERE, "web")
PLACE_POINTS = [10, 7, 5, 3, 2, 1]
SEATS = 6   # Blaze keeps 2 of its 8 seats for browser players, so 6 agents per world

COMMENTATOR = """You are the Ember commentator for Blaze Battle Royale, a live tournament where AI bots,
each with its own personality, race through a burning maze city. Call the action like an excited
but witty sports commentator. Use the bots' names. One or two short sentences, under 40 words, no
emoji, no lists. Only describe what the events say happened."""


class Arena:
    """One Blaze server process (or an external MCP URL)."""

    def __init__(self, idx, args, url=None):
        self.idx = idx
        self.args = args
        self.proc = None
        self.external = url is not None
        gp = args.base_port + 2 * idx
        self.game_port, self.mcp_port = gp, gp + 1
        self.url = url or "http://127.0.0.1:%d/mcp" % self.mcp_port
        self.spectate = None if self.external else "http://127.0.0.1:%d/spectate" % self.game_port

    def start(self):
        if self.external:
            return
        self.stop()
        os.makedirs(os.path.join(HERE, "arenas"), exist_ok=True)
        db = os.path.join(HERE, "arenas", "arena-%d.db" % self.idx)
        cmd = [self.args.server_bin, "-addr", "127.0.0.1:%d" % self.game_port,
               "-mcp-addr", "127.0.0.1:%d" % self.mcp_port, "-results", db, "-workers", "2"]
        if self.args.blaze_root:
            cmd += ["-root", self.args.blaze_root]
        log = open(os.path.join(HERE, "arenas", "arena-%d.log" % self.idx), "a")
        self.proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
        import mcp_client
        for _ in range(50):
            time.sleep(0.2)
            try:
                c = mcp_client.MCPClient(self.url, timeout=2)
                c.initialize()
                return
            except Exception:
                pass
        raise SystemExit("arena %d did not start; see arenas/arena-%d.log" % (self.idx, self.idx))

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None


class Royale:
    def __init__(self, args):
        self.args = args
        self.ledger = Ledger(cap=args.cap)
        self.use_ember = not args.no_ember
        if self.use_ember and not os.environ.get("MODEL_API_KEY"):
            raise SystemExit("MODEL_API_KEY is not set. Run: export MODEL_API_KEY=<your Meta Model API key>\n"
                             "(or add --no-ember for a rules-only test run)")
        urls = args.arena or []
        n = len(urls) if urls else (args.arenas or -(-args.bots // SEATS))
        self.arenas = [Arena(i, args, urls[i] if urls else None) for i in range(n)]
        self.names = roster(args.bots)
        self.points = collections.Counter()
        self.history = []           # finished heats
        self.lock = threading.Lock()
        self.events = collections.deque(maxlen=60)
        self.pending = []           # events since the last commentary
        self.commentary = collections.deque(maxlen=30)
        self.heat_label = "warming up"
        self.heat_end = None
        self.current = []           # [(arena, [bots])]
        self.phase = "setup"
        self.stop = threading.Event()
        os.makedirs(os.path.join(HERE, "results"), exist_ok=True)
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        self.log = open(os.path.join(HERE, "results", "events-%s.jsonl" % self.stamp), "a")

    # ---------------------------------------------------------------- events
    def on_event(self, name, kind, text, **kw):
        e = {"t": round(time.time(), 1), "bot": name, "kind": kind, "text": text}
        e.update(kw)
        with self.lock:
            if kind != "intent":
                self.events.append(e)
            self.pending.append(e)
            self.log.write(json.dumps(e) + "\n")
            self.log.flush()

    def ember(self, max_tokens=300):
        return Ember(self.ledger, effort=self.args.effort, max_tokens=max_tokens, timeout=self.args.ember_timeout)

    # ---------------------------------------------------------------- commentator
    def commentator(self):
        if not self.use_ember or self.args.no_commentator:
            return
        try:
            em = self.ember(max_tokens=400)
        except EmberError:
            return
        while not self.stop.wait(self.args.comment_every):
            with self.lock:
                evs, self.pending = self.pending, []
            evs = [e for e in evs if e["kind"] in ("level", "life", "out", "say", "intent", "cap")]
            if not evs or self.phase != "heat":
                continue
            top = self.standings_live()[:3]
            lines = ["%s: %s" % (e["bot"], e["text"]) for e in evs[-14:]]
            user = ("Heat: %s. Leaders right now: %s.\nWhat just happened:\n%s" % (
                self.heat_label, ", ".join("%s (level %d, %d points)" % (b["name"], b["level"], b["score"])
                                           for b in top), "\n".join(lines)))
            try:
                text, info = em.chat([{"role": "system", "content": COMMENTATOR},
                                      {"role": "user", "content": user}], role="commentator", max_tokens=400)
            except CapReached:
                self.commentary.append({"t": time.time(), "text": "(The commentator has run out of Ember budget.)"})
                return
            except EmberError:
                continue
            text = " ".join(text.strip().split())[:300]
            if text:
                self.commentary.append({"t": time.time(), "text": text})
                self.log.write(json.dumps({"t": time.time(), "kind": "commentary", "text": text}) + "\n")

    # ---------------------------------------------------------------- ranking
    @staticmethod
    def rank_key(b):
        p = b.public()
        alive = 1 if p["state"] != "out" else 0
        return (-p["best"]["level"], -p["best"]["score"], -alive, -(p["out_at"] or 9e12))

    def standings_live(self):
        bots = [b for _, bs in self.current for b in bs]
        out = []
        for b in sorted(bots, key=lambda b: (-(b.public()["level"]), -(b.public()["score"]))):
            out.append(b.public())
        return out

    # ---------------------------------------------------------------- heats
    def run_heat(self, label, groups):
        """groups: list of name lists, one per arena. Returns ranked names per arena."""
        self.heat_label = label
        self.phase = "starting"
        for i in range(len(groups)):
            self.arenas[i].start()
        deadline = time.time() + self.args.minutes * 60
        self.heat_end = deadline
        factory = self.ember if self.use_ember else None
        self.current = []
        for i, names in enumerate(groups):
            bots = [Bot(n, self.arenas[i].url, ember_factory=factory, period=self.args.period,
                        on_event=self.on_event, deadline=deadline) for n in names]
            self.current.append((self.arenas[i], bots))
        self.on_event("royale", "heat", "%s begins: %s" % (label, ", ".join(sum(groups, []))))
        self.phase = "heat"
        for _, bots in self.current:
            for b in bots:
                b.start()
                time.sleep(0.15)
        while time.time() < deadline and not self.stop.is_set():
            if all(b.stop.is_set() for _, bots in self.current for b in bots):
                break
            time.sleep(0.5)
        for _, bots in self.current:
            for b in bots:
                b.stop.set()
        for _, bots in self.current:
            for b in bots:
                b.join(20)
        result = []
        for arena, bots in self.current:
            ranked = sorted(bots, key=self.rank_key)
            rows = []
            for place, b in enumerate(ranked):
                p = b.public()
                pts = PLACE_POINTS[place] if place < len(PLACE_POINTS) else 0
                self.points[b.name] += pts
                rows.append({"place": place + 1, "name": b.name, "persona": b.persona, "level": p["best"]["level"],
                             "score": p["best"]["score"], "out": p["state"] == "out", "points": pts,
                             "ember_calls": p["ember_calls"], "moves": p["moves"]})
            result.append(rows)
            self.on_event("royale", "heat_end", "%s, arena %d winner: %s (level %d, %d points)" % (
                label, arena.idx + 1, rows[0]["name"], rows[0]["level"], rows[0]["score"]))
        self.history.append({"label": label, "arenas": result})
        self.phase = "between"
        return result

    def run(self):
        threading.Thread(target=self.writer, daemon=True).start()
        threading.Thread(target=self.commentator, daemon=True).start()
        if self.args.serve:
            threading.Thread(target=self.serve, daemon=True).start()
            print("leaderboard: http://127.0.0.1:%d/" % self.args.port, flush=True)
        try:
            k = len(self.arenas)
            groups = [self.names[i::k] for i in range(k)]
            groups = [g[:SEATS] for g in groups if g]
            dropped = len(self.names) - sum(len(g) for g in groups)
            if dropped:
                print("note: %d bots don't fit (%d arenas x %d seats)" % (dropped, k, SEATS))
            res = self.run_heat("Round 1", groups)
            if k > 1 and not self.stop.is_set():
                per = max(1, SEATS // len(res))
                finalists = [r["name"] for rows in res for r in rows[:per]]
                extra = sorted((r for rows in res for r in rows[per:]), key=lambda r: (-r["level"], -r["score"]))
                finalists = (finalists + [r["name"] for r in extra])[:SEATS]
                time.sleep(3)
                self.run_heat("Final", [finalists])
            self.phase = "done"
            self.heat_label = "Tournament over"
            self.finish()
            time.sleep(2 if not self.args.linger else self.args.linger)
        finally:
            self.stop.set()
            for a in self.arenas:
                a.stop()

    def finish(self):
        champ = self.history[-1]["arenas"][0][0] if self.history else None
        spend = self.ledger.snapshot()
        out = {"stamp": self.stamp, "champion": champ, "season_points": self.points.most_common(),
               "heats": self.history, "spend": spend, "args": {k: v for k, v in vars(self.args).items() if k != "arena"}}
        path = os.path.join(HERE, "results", "tournament-%s.json" % self.stamp)
        json.dump(out, open(path, "w"), indent=1)
        if champ:
            self.on_event("royale", "champion", "%s wins Blaze Battle Royale (level %d, %d points)" % (
                champ["name"], champ["level"], champ["score"]))
        print("\nChampion: %s" % (champ and champ["name"]))
        print("Ember spend: $%.4f of $%.2f cap, %d calls" % (spend["spent"], spend["cap"], spend.get("calls", 0)))
        print("Results: %s" % path)

    # ---------------------------------------------------------------- live state
    def live(self):
        arenas = []
        for arena, bots in self.current:
            arenas.append({"id": arena.idx + 1, "spectate": arena.spectate,
                           "bots": sorted((b.public() for b in bots), key=lambda p: (-p["level"], -p["score"]))})
        spend = self.ledger.snapshot()
        lat = sorted(x for _, bots in self.current for b in bots for x in b.ember_lat)
        return {"now": time.time(), "phase": self.phase, "heat": self.heat_label,
                "time_left": max(0, int(self.heat_end - time.time())) if self.heat_end and self.phase == "heat" else None,
                "arenas": arenas, "leaderboard": self.standings_live(),
                "season": [{"name": n, "points": p, "color": PERSONAS[n.split("-")[0]]["color"]}
                           for n, p in self.points.most_common()],
                "history": self.history, "events": list(self.events)[-25:], "commentary": list(self.commentary)[-12:],
                "spend": {"spent": spend.get("spent", 0), "cap": spend.get("cap"), "calls": spend.get("calls", 0),
                          "tokens_in": spend.get("in", 0), "tokens_out": spend.get("out", 0),
                          "by_role": spend.get("by_role", {})},
                "ember": {"on": self.use_ember, "model": os.environ.get("EMBER_MODEL", "muse-ember-1.0"),
                          "median_ms": int(1000 * lat[len(lat) // 2]) if lat else None}}

    def writer(self):
        path = os.path.join(WEB, "live.json")
        while not self.stop.is_set():
            try:
                tmp = path + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(self.live(), f)
                os.replace(tmp, path)
            except Exception as e:  # never let the display stop the games
                print("live.json:", e, file=sys.stderr)
            time.sleep(1)

    def serve(self):
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def end_headers(self):
                self.send_header("Cache-Control", "no-store")
                super().end_headers()
        ThreadingHTTPServer(("127.0.0.1", self.args.port), partial(Quiet, directory=WEB)).serve_forever()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server-bin", help="path to blaze-server (cargo build --release in the blaze repo)")
    ap.add_argument("--blaze-root", help="blaze repo root, for the spectate page assets")
    ap.add_argument("--arena", action="append", help="MCP URL of an already running Blaze server (repeat)")
    ap.add_argument("--arenas", type=int, default=0, help="arenas to launch with --server-bin (6 bots each; 0 = enough for --bots)")
    ap.add_argument("--base-port", type=int, default=9100)
    ap.add_argument("--bots", type=int, default=32)
    ap.add_argument("--minutes", type=float, default=6.0, help="length of each heat")
    ap.add_argument("--period", type=float, default=4.0, help="seconds between Ember calls per bot")
    ap.add_argument("--cap", type=float, default=DEFAULT_CAP,
                    help="hard Ember spending cap in dollars for everything in this folder (max %.0f)" % HARD_CEILING)
    ap.add_argument("--effort", default=os.environ.get("EMBER_EFFORT", "minimal"),
                    help="reasoning_effort for Ember (none|minimal|low|...|default)")
    ap.add_argument("--ember-timeout", type=float, default=8.0)
    ap.add_argument("--comment-every", type=float, default=12.0)
    ap.add_argument("--no-commentator", action="store_true")
    ap.add_argument("--no-ember", action="store_true", help="rules-only bots (no API calls)")
    ap.add_argument("--port", type=int, default=8099, help="leaderboard port")
    ap.add_argument("--no-serve", dest="serve", action="store_false")
    ap.add_argument("--linger", type=float, default=0, help="seconds to keep the page up after the final")
    args = ap.parse_args()
    if not args.arena and not args.server_bin:
        ap.error("pass --server-bin (to launch arenas) or --arena <MCP URL>")
    if args.cap > HARD_CEILING:
        ap.error("--cap can't be above $%.2f" % HARD_CEILING)
    r = Royale(args)
    signal.signal(signal.SIGINT, lambda *a: r.stop.set())
    r.run()


if __name__ == "__main__":
    main()
