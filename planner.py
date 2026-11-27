"""Fast in-memory Blaze planner, steered by an intent.

Ported from fast-bot/planner.py (same pathing ideas), changed for many bots in one process
and for multiplayer: one Planner per bot, maps in memory, rival fire trails are lethal,
and the target depends on the bot's current intent (set by Ember, the strategist):

  exit     known exit if it is open, else gems (the gem leader opens the exits), else explore
  gems     nearest gems, weighted by cluster; exit once no gems are in view
  drops    like gems but braver (smaller fear radius)
  explore  unknown ground
  hunt     get close to the nearest rival, so they run into your trail
  safe     stay far from enemies and rivals, take only gems that are safe
  shadow   follow the score leader at a distance
"""
import collections, heapq, random

DIRS = {"up": (0, -1), "down": (0, 1), "left": (-1, 0), "right": (1, 0),
        "up_left": (-1, -1), "up_right": (1, -1), "down_left": (-1, 1), "down_right": (1, 1)}
V2D = {v: k for k, v in DIRS.items()}
FIRE = "obptaeiu"          # red fire owned by slots 0..7
VW = 20

DEFAULTS = dict(
    ms_per_cell=220, max_cells=6, max_cells_danger=3, max_cells_inv=10,
    kill_r=2, fear_r=4, line_r=6, fear_w=6, line_w=10, unknown_cost=1.6,
    death_w=4, trail_w=40, gem_w=1.0, drop_w=1.0, explore_w=1.5, rival_w=0.0,
    shadow_dist=6, jitter=0.0, seq=4, seq_danger=2,
)


def tdist(a, b, n):
    d = abs(a - b)
    return min(d, n - d) if n else d


class LevelMap:
    def __init__(self):
        self.cells = {}
        self.exits = set()
        self.visits = collections.Counter()
        self.deaths = collections.Counter()
        self.W = None
        self.H = None


class Planner:
    def __init__(self, params=None, seed=None):
        self.P = dict(DEFAULTS)
        self.P.update(params or {})
        self.maps = {}
        self.last = None
        self.speed = self.P["ms_per_cell"]
        self.rng = random.Random(seed)

    def set_params(self, params):
        self.P.update(params)

    # ------------------------------------------------------------------ helpers
    def _norm(self, m, x, y):
        if m.W:
            x %= m.W
        if m.H:
            y %= m.H
        return x, y

    @staticmethod
    def flow(obs):
        """Flow action for a non-playing state, or None while playing."""
        st = str(obs.get("state", ""))
        if "over" in st:
            return "game_over"
        if "complete" in st:
            return "next"
        if "wait" in st:
            return "start"
        if "dead" in st or "spawn" in st:
            return "wait"
        return None

    def my_slot(self, obs, name):
        for p in obs.get("players") or []:
            if str(p.get("name", "")).lower() == name.lower():
                return p.get("slot")
        return None

    # ------------------------------------------------------------------ main
    def plan(self, obs, intent="gems", slot=None):
        """Returns dict: seq [[dir, ms], ...], goal, to, info."""
        P = self.P
        lv = obs.get("level", 0)
        m = self.maps.setdefault(lv, LevelMap())
        pos = obs["position"]
        px, py = pos["x"], pos["y"]
        own_fire = FIRE[slot] if slot is not None and 0 <= slot < 8 else None

        # learn wrap and speed from the last move
        last = self.last
        if last and last["level"] == lv:
            lx, ly = last["x"], last["y"]
            jx, jy = px - lx, py - ly
            same_life = obs.get("lives") == last.get("lives") and not obs.get("invincible")
            if abs(jx) > 40 and not m.W and same_life and jx < 0:
                m.W = max(lx, px) + 1
            if abs(jy) > 40 and not m.H and same_life and jy < 0:
                m.H = max(ly, py) + 1
            moved = max(abs(jx), abs(jy))
            if last.get("cells") and 0 < moved < 40 and obs.get("lives") == last.get("lives"):
                r = (last["ms"] + 120) / max(moved, 1)
                self.speed = max(80, min(260, 0.7 * self.speed + 0.3 * r))
            elif last.get("cells") and moved == 0:
                dx, dy = DIRS[last["dir"]]
                k = self._norm(m, px + dx, py + dy)
                if m.cells.get(k, ".") != "#":
                    m.cells[k] = "x"
            if obs.get("lives", 3) < last.get("lives", 3):
                m.deaths[(lx, ly)] += 1

        # merge viewport; fire is transient, so it goes to a per-call danger set, not the map
        vp = obs.get("viewport") or {}
        ox, oy, cs = vp.get("origin_x", px - 10), vp.get("origin_y", py - 9), vp.get("cells", "")
        fire_cells = set()
        for i, ch in enumerate(cs):
            c = self._norm(m, ox + i % VW, oy + i // VW)
            if ch in FIRE:
                if ch != own_fire:
                    fire_cells.add(c)
                ch = "."
            elif ch in "@fE*":
                ch = "."
            m.cells[c] = ch
        vis = obs.get("visible") or {}
        for e in vis.get("exits") or []:
            m.exits.add(self._norm(m, px + e["dx"], py + e["dy"]))
        m.visits[self._norm(m, px, py)] += 1

        inv = bool(obs.get("invincible"))
        enemies = [(px + e["dx"], py + e["dy"], e.get("type", "")) for e in (vis.get("enemies") or [])]
        rivals = [(px + o["dx"], py + o["dy"], o) for o in (vis.get("other_players") or [])
                  if not o.get("dead")]
        gems = [self._norm(m, px + g["dx"], py + g["dy"]) for g in (vis.get("gems") or [])]

        brave = intent in ("drops", "hunt")
        fear_r = max(1, P["fear_r"] - (1 if brave else 0))
        kill_r = P["kill_r"] if intent != "safe" else P["kill_r"] + 1

        block, cost = set(), collections.Counter()
        for ex, ey, et in enemies:
            for dx in range(-fear_r, fear_r + 1):
                for dy in range(-fear_r, fear_r + 1):
                    c = self._norm(m, ex + dx, ey + dy)
                    d = max(abs(dx), abs(dy))
                    if d <= kill_r and not inv:
                        block.add(c)
                    cost[c] += (fear_r + 1 - d) * P["fear_w"] * (2 if intent == "safe" else 1)
            if et in ("orange", ""):
                for s in range(-P["line_r"], P["line_r"] + 1):
                    cost[self._norm(m, ex + s, ey)] += P["line_w"]
                    cost[self._norm(m, ex, ey + s)] += P["line_w"]
        for c in fire_cells:
            if not inv:
                block.add(c)
            cost[c] += P["trail_w"]
        rw = P["rival_w"] + (4.0 if intent == "safe" else 0.0)
        if rw > 0:
            for rx, ry, _ in rivals:
                for dx in range(-3, 4):
                    for dy in range(-3, 4):
                        cost[self._norm(m, rx + dx, ry + dy)] += rw * (4 - max(abs(dx), abs(dy)))
        for c, n in m.deaths.items():
            cost[c] += P["death_w"] * n

        start = self._norm(m, px, py)
        v = obs.get("velocity") or {}
        sx = (v.get("x", 0) > 1) - (v.get("x", 0) < -1)
        sy = (v.get("y", 0) > 1) - (v.get("y", 0) < -1)
        if (sx or sy) and m.cells.get(self._norm(m, px + sx, py + sy), ".") not in "#x":
            start = self._norm(m, px + sx, py + sy)
        block.discard(start)

        # ----- targets from intent
        can_exit = obs.get("can_exit", True)
        exits = set(m.exits) if can_exit else set()
        targets, goal = set(), None
        if intent == "hunt" and rivals:
            rx, ry, _ = min(rivals, key=lambda r: max(abs(r[0] - px), abs(r[1] - py)))
            # aim one or two cells beside the rival, never onto it
            ring = [self._norm(m, rx + a, ry + b) for a in (-2, -1, 0, 1, 2) for b in (-2, -1, 0, 1, 2)
                    if max(abs(a), abs(b)) == 2]
            targets, goal = set(ring), "hunt"
        elif intent == "shadow" and rivals:
            rx, ry, ro = max(rivals, key=lambda r: r[2].get("score", 0))
            dd = P["shadow_dist"]
            ring = [self._norm(m, rx + a, ry + b) for a in range(-dd, dd + 1) for b in range(-dd, dd + 1)
                    if max(abs(a), abs(b)) == dd]
            targets, goal = set(ring), "shadow"
        if not targets and exits and (intent in ("exit", "explore", "safe", "hunt", "shadow")
                                      or (intent in ("gems", "drops") and not gems)):
            targets, goal = exits, "exit"
        if not targets and gems and intent != "explore":
            # drop gems that sit in danger
            safe = [g for g in gems if g not in block and cost.get(g, 0) < 30 * (2 if brave else 1)]
            if safe:
                targets, goal = set(safe), "gem"
        if not targets and exits:
            targets, goal = exits, "exit"

        # ----- Dijkstra (with heuristic toward targets)
        dist = {start: 0.0}
        prev = {}
        pq = [(0.0, start)]
        best, best_score, seen = None, None, 0
        jitter = P["jitter"]
        while pq and seen < 30000:
            _, c = heapq.heappop(pq)
            d = dist[c]
            seen += 1
            if targets and c in targets:
                best = c
                break
            if not targets and c != start and c not in m.cells:
                sc = d + (self.rng.random() * 6 * jitter)
                if best_score is None or sc < best_score:
                    best_score, best = sc, c
                continue
            for dx, dy in DIRS.values():
                n = self._norm(m, c[0] + dx, c[1] + dy)
                if not self._pass(m, n) or n in block:
                    continue
                if dx and dy and (not self._pass(m, self._norm(m, c[0] + dx, c[1]))
                                  or not self._pass(m, self._norm(m, c[0], c[1] + dy))):
                    continue
                step = 1.4 if dx and dy else 1.0
                if n not in m.cells:
                    step *= P["unknown_cost"]
                step += cost.get(n, 0) + 0.3 * min(m.visits.get(n, 0), 10)
                nd = d + step
                if nd < dist.get(n, 1e18):
                    dist[n] = nd
                    prev[n] = c
                    h = 0
                    if targets and len(targets) < 60:
                        h = min(max(tdist(n[0], t[0], m.W), tdist(n[1], t[1], m.H)) for t in targets)
                    heapq.heappush(pq, (nd + h, n))
            if not targets and best is not None and seen > 4000:
                break

        info = {"en": len(enemies), "riv": len(rivals), "gems": len(gems), "fire": len(fire_cells),
                "spd": round(self.speed)}
        if best is None:
            if enemies:
                ex, ey, _ = min(enemies, key=lambda e: abs(e[0] - px) + abs(e[1] - py))
                dname = V2D.get(((px > ex) - (px < ex), (py > ey) - (py < ey))) or "up"
            else:
                dname = ["up", "left", "down", "right"][sum(m.visits.values()) % 4]
            self.last = {"level": lv, "x": px, "y": py, "dir": dname, "ms": 250, "cells": 0,
                         "lives": obs.get("lives")}
            return {"seq": [[dname, 250]], "goal": "flee" if enemies else "wiggle", "to": None, "info": info}

        path = [best]
        while path[-1] != start:
            path.append(prev[path[-1]])
        path.reverse()
        near = any(max(abs(ex - px), abs(ey - py)) <= 8 for ex, ey, _ in enemies) or bool(fire_cells)
        cap = P["max_cells_danger"] if near and not inv else (P["max_cells_inv"] if inv else P["max_cells"])
        pidx = {c: i for i, c in enumerate(path)}
        nseg = int(P["seq_danger"] if near and not inv else P["seq"])
        seq = []
        cur, ci = start, 0
        ncell = 0
        for _ in range(nseg):
            bestd = None
            for dn, (dx, dy) in DIRS.items():
                x, y = cur
                top, topk, tc = ci, 0, None
                for k in range(1, cap + 1):
                    if dx and dy and (not self._pass(m, self._norm(m, x + dx, y))
                                      or not self._pass(m, self._norm(m, x, y + dy))):
                        break
                    x, y = self._norm(m, x + dx, y + dy)
                    if not self._pass(m, (x, y)) or (x, y) in block:
                        break
                    on = pidx.get((x, y), -1)
                    near_i = max((pidx.get(self._norm(m, x + a, y + b), -1) for a in (-1, 0, 1) for b in (-1, 0, 1)),
                                 default=-1)
                    gi = on if on >= 0 else (near_i - 1 if near_i > ci + k else -1)
                    if gi > top:
                        top, topk, tc = gi, k, (x, y)
                if topk and (bestd is None or top > bestd[0]):
                    bestd = (top, topk, dn, tc)
            if bestd is None:
                break
            ci, ncell, dname, cur = bestd
            sp = self.speed * (1.41 if "_" in dname else 1.0)
            seq.append([dname, int(min(2000, max(60, sp * (ncell - 1) - 40))), ncell])
            if cur == path[-1]:
                break
        if not seq:
            a, b = path[0], path[1] if len(path) > 1 else path[0]
            d0 = (max(-1, min(1, b[0] - a[0])), max(-1, min(1, b[1] - a[1])))
            seq = [[V2D.get(d0, "up"), 200, 1]]
        dname, ms, ncell = seq[0]
        self.last = {"level": lv, "x": px, "y": py, "dir": dname, "ms": ms,
                     "cells": ncell if len(seq) == 1 else 0, "lives": obs.get("lives")}
        return {"seq": [s[:2] for s in seq], "goal": goal or "explore", "to": list(best),
                "len": len(path), "info": info}

    @staticmethod
    def _pass(m, c):
        ch = m.cells.get(c)
        return ch is None or ch not in "#x"
