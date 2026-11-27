"""One Blaze Battle Royale bot: a fast planner that moves, steered by Muse Ember.

Two threads per bot:
  - mover: observe -> planner -> move, as fast as the server answers (no model in this loop)
  - strategist: every few seconds, and at once after a lost life or a new level, Ember reads a
    short summary of the bot's situation and answers with an intent and a line in character.
If Ember is slow, failing, or the spending cap is reached, the bot keeps playing on its last
intent (or its persona's default).
"""
import json, threading, time, traceback

import mcp_client
from ember import CapReached, EmberError, extract_json
from personas import INTENTS, PERSONAS, persona_of
from planner import Planner

RULES = """Blaze is a real-time maze game. Up to 8 players share one city. Each level: collect gems,
reach the exit. Only the player(s) with the highest gem score among active players see and can
use the exits (from level 6 strictly; earlier levels the exit opens as you lead). Enemies kill on
contact; orange enemies chase. Every player leaves a burning trail: a rival's trail hurts you, yours
hurts them. 3 lives; lose them all and you are out. Walking without gems slowly costs score."""

INTENT_HELP = ("exit=go for the exit, gems=collect gems, drops=gems but braver, explore=find unknown ground, "
               "hunt=get close to the nearest rival so they hit your trail, safe=keep far from enemies and rivals, "
               "shadow=follow the score leader at a distance")


def summarize(obs, me, last_intent, events):
    vis = obs.get("visible") or {}
    pos = obs.get("position") or {}
    en = vis.get("enemies") or []
    near = sorted(en, key=lambda e: max(abs(e["dx"]), abs(e["dy"])))[:4]
    riv = [o for o in (vis.get("other_players") or []) if not o.get("dead")]
    players = sorted(obs.get("players") or [], key=lambda p: (-p.get("level", 0), -p.get("score", 0)))
    rank = next((i + 1 for i, p in enumerate(players) if str(p.get("name", "")).lower() == me.lower()), None)
    table = ", ".join("%s L%d %d pts %d lives%s" % (p.get("name"), p.get("level", 0), p.get("score", 0),
                                                   p.get("lives", 0), " (out)" if p.get("dead") else "")
                      for p in players[:8])
    lines = [
        "You: level %s, score %s, lives %s, rank %s of %d, invincible %s, exit open for you: %s, exit seen: %s." % (
            obs.get("level"), obs.get("score"), obs.get("lives"), rank, len(players),
            bool(obs.get("invincible")), obs.get("can_exit"), bool(vis.get("exits"))),
        "Gems in view: %d. Enemies in view: %s." % (
            len(vis.get("gems") or []),
            ", ".join("%s %d cells" % (e.get("type") or "plain", max(abs(e["dx"]), abs(e["dy"]))) for e in near) or "none"),
        "Rivals in view: %s." % (", ".join("slot %d score %d %d cells away" % (
            o["slot"], o.get("score", 0), max(abs(o["dx"]), abs(o["dy"]))) for o in riv) or "none"),
        "Standings: %s." % table,
        "Your current intent: %s." % last_intent,
    ]
    if events:
        lines.append("Since last time: %s." % "; ".join(events[-5:]))
    cells = (obs.get("viewport") or {}).get("cells", "")
    if cells:
        lines.append("View (20 wide, @ is you, # wall, letters are fire trails):\n" +
                     "\n".join(cells[i:i + 20] for i in range(0, len(cells), 20)))
    return "\n".join(lines)


class Bot:
    def __init__(self, name, mcp_url, ember_factory=None, model="muse-ember-1.0", harness="blaze-royale",
                 period=4.0, on_event=None, deadline=None, slot=None):
        self.name = name
        self.persona = persona_of(name)
        self.spec = PERSONAS[self.persona]
        self.mcp_url = mcp_url
        self.ident = {"name": name, "model": model if ember_factory else "rules", "harness": harness}
        if slot is not None:
            self.ident["slot"] = slot
        self.planner = Planner(self.spec["params"], seed=hash(name) & 0xffff)
        self.intent = self.spec["default"]
        self.say = ""
        self.period = period
        self.ember_factory = ember_factory
        self.on_event = on_event or (lambda *a, **k: None)
        self.deadline = deadline
        self.obs = None
        self.slot = None
        self.events = []
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.state = "joining"
        self.best = {"level": 0, "score": 0}
        self.started = None
        self.out_at = None
        self.lives_seen = None
        self.level_seen = None
        self.ember_calls = 0
        self.ember_fail = 0
        self.ember_lat = []
        self.moves = 0
        self.error = None

    # --------------------------------------------------------------- public
    def public(self):
        o = self.obs or {}
        return {"name": self.name, "persona": self.persona, "color": self.spec["color"],
                "level": o.get("level", 0), "score": o.get("score", 0), "lives": o.get("lives", 0),
                "state": self.state, "intent": self.intent, "say": self.say, "best": self.best,
                "out_at": self.out_at, "started": self.started, "moves": self.moves,
                "ember_calls": self.ember_calls, "ember_fail": self.ember_fail,
                "ember_ms": int(1000 * sorted(self.ember_lat)[len(self.ember_lat) // 2]) if self.ember_lat else None,
                "slot": self.slot, "error": self.error}

    def start(self):
        self.t_move = threading.Thread(target=self._mover, name=self.name + "-mover", daemon=True)
        self.t_move.start()
        if self.ember_factory:
            self.t_strat = threading.Thread(target=self._strategist, name=self.name + "-ember", daemon=True)
            self.t_strat.start()

    def join(self, timeout=None):
        self.t_move.join(timeout)

    # --------------------------------------------------------------- events
    def _event(self, kind, text, **kw):
        self.events.append(text)
        self.events = self.events[-12:]
        self.on_event(self.name, kind, text, **kw)

    def _track(self, obs):
        self.obs = obs
        if self.slot is None:
            self.slot = self.planner.my_slot(obs, self.name)
        lv, lives, sc = obs.get("level", 0), obs.get("lives"), obs.get("score", 0)
        if (lv, sc) > (self.best["level"], self.best["score"]):
            self.best = {"level": lv, "score": sc}
        if self.level_seen is not None and lv > self.level_seen:
            self._event("level", "%s reached level %d" % (self.name, lv), level=lv)
            self.wake.set()
        if self.lives_seen is not None and lives is not None and lives < self.lives_seen:
            self._event("life", "%s lost a life (%d left) on level %d" % (self.name, lives, lv), lives=lives)
            self.wake.set()
        self.level_seen, self.lives_seen = lv, lives

    # --------------------------------------------------------------- mover
    def _call(self, tool, **kw):
        a = dict(self.ident)
        a.update(kw)
        return self.client.call(tool, a)

    def _mover(self):
        try:
            self.client = mcp_client.MCPClient(self.mcp_url, timeout=15)
            self.client.initialize()
            obs = None
            for attempt in range(20):
                try:
                    obs = self._call("observe")
                    break
                except mcp_client.MCPError as e:
                    self.error = str(e)[:120]
                    time.sleep(1.0)
            if obs is None:
                self.state = "no_seat"
                return
            self.error = None
            self.started = time.time()
            self._track(obs)
            while not self.stop.is_set():
                if self.deadline and time.time() > self.deadline:
                    self.state = "time"
                    break
                fl = Planner.flow(obs)
                if fl == "game_over":
                    self.state = "out"
                    self.out_at = time.time()
                    self._event("out", "%s is OUT at level %d with %d points" % (
                        self.name, self.best["level"], self.best["score"]), level=self.best["level"])
                    break
                if fl in ("start", "next"):
                    self.state = "playing"
                    obs = self._call("game_action", action=fl)
                elif fl == "wait":
                    self.state = str(obs.get("state"))
                    time.sleep(0.15)
                    obs = self._call("observe")
                else:
                    self.state = "playing"
                    plan = self.planner.plan(obs, self.intent, self.slot)
                    seq = plan["seq"][:2]
                    for d, ms in seq:
                        obs = self._call("move", direction=d, duration_ms=int(ms))
                        self.moves += 1
                        if Planner.flow(obs) or (obs.get("visible") or {}).get("enemies"):
                            break
                self._track(obs)
        except Exception as e:  # keep the tournament going
            self.error = "%s: %s" % (type(e).__name__, str(e)[:160])
            self.state = "error"
            traceback.print_exc()
        finally:
            self.stop.set()
            self.wake.set()

    # --------------------------------------------------------------- strategist
    def _strategist(self):
        try:
            ember = self.ember_factory()
        except EmberError as e:
            self.error = str(e)
            return
        system = ("%s\n\n%s\n\nEach turn you get your situation. Reply with ONE JSON object only:\n"
                  '{"intent": "<one of: %s>", "say": "<at most 12 words, in character>"}\n'
                  "Intents: %s." % (self.spec["voice"], RULES, "|".join(INTENTS), INTENT_HELP))
        while not self.stop.is_set():
            self.wake.wait(self.period)
            self.wake.clear()
            if self.stop.is_set() or not self.obs or Planner.flow(self.obs) not in (None, "wait"):
                continue
            user = summarize(self.obs, self.name, self.intent, self.events)
            self.events = []
            try:
                text, info = ember.chat([{"role": "system", "content": system},
                                         {"role": "user", "content": user}], role="bot", max_tokens=200)
            except CapReached:
                self.say = "(out of Ember budget, playing on instinct)"
                self._event("cap", "%s: spending cap reached, Ember paused" % self.name)
                return
            except EmberError as e:
                self.ember_fail += 1
                self.error = str(e)[:120]
                time.sleep(min(10, 2 * self.ember_fail))
                continue
            self.ember_calls += 1
            self.ember_lat.append(info["latency_s"])
            self.ember_lat = self.ember_lat[-50:]
            d = extract_json(text) or {}
            it = str(d.get("intent", "")).strip().lower()
            if it in INTENTS:
                if it != self.intent:
                    self._event("intent", "%s switches to %s" % (self.name, it), intent=it)
                self.intent = it
            else:
                self.ember_fail += 1
            say = str(d.get("say", "")).strip()
            if say:
                self.say = say[:90]
                self.on_event(self.name, "say", say[:90])
