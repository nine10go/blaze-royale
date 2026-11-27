#!/usr/bin/env python3
"""Check your Muse Ember key and measure speed and cost per bot decision.

    export MODEL_API_KEY=...     # Meta Model API key (starts with LLM_)
    python3 ember_check.py       # about 12 small calls, well under one cent

Prints: whether the key works, the models it can see, and for each reasoning effort the
median latency, tokens and dollars per decision, plus what a full tournament would cost.
Every call counts against the same spending cap as the tournament (spend.json).
"""
import argparse, json, os, statistics, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from bot import RULES, INTENT_HELP, summarize  # noqa: E402
from ember import CapReached, DEFAULT_CAP, Ember, EmberError, Ledger, extract_json  # noqa: E402
from personas import INTENTS, PERSONAS  # noqa: E402

SAMPLE = {"level": 2, "score": 214, "lives": 2, "can_exit": False, "state": "playing",
          "position": {"x": 67, "y": 51},
          "viewport": {"origin_x": 57, "origin_y": 42, "cells":
                       "..##.......#####..##...........#...###.............#....####.###.......#..######..#"
                       "....#...........#.......#...................#...................#.................#"
                       "........................@.............................................##########.."
                       "..##..######..##.#....#.........#.##.#....#............#.#....#####..###.#####....."
                       "............................................................bbbb................"},
          "visible": {"gems": [{"dx": 3, "dy": 3}, {"dx": -6, "dy": -7}, {"dx": 2, "dy": 4}],
                      "enemies": [{"dx": 4, "dy": -1, "type": "orange"}, {"dx": -7, "dy": 5}],
                      "other_players": [{"slot": 1, "dx": -3, "dy": 6, "score": 390, "lives": 3}]},
          "players": [{"slot": 0, "name": "Hunter", "level": 2, "score": 214, "lives": 2},
                      {"slot": 1, "name": "Hoarder", "level": 2, "score": 390, "lives": 3},
                      {"slot": 2, "name": "Coward", "level": 1, "score": 80, "lives": 1}]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--efforts", default="none,minimal,low,default",
                    help="reasoning_effort values to try, comma separated ('default' = leave it out)")
    ap.add_argument("--n", type=int, default=3, help="calls per effort")
    ap.add_argument("--cap", type=float, default=DEFAULT_CAP)
    a = ap.parse_args()
    if not os.environ.get("MODEL_API_KEY"):
        sys.exit("Set your key first:  export MODEL_API_KEY=<your Meta Model API key>")
    ledger = Ledger(cap=a.cap)
    before = ledger.snapshot()["spent"]
    e = Ember(ledger, effort=None, timeout=30)
    print("Endpoint:", e.base.geturl(), " model:", e.model)
    try:
        models = e.list_models()
        print("Key works. Models it can see:", ", ".join(m for m in models if m)[:400])
        if e.model not in models:
            print("  note: %s is not in that list; set EMBER_MODEL to the right id if calls fail" % e.model)
    except EmberError as err:
        print("Listing models failed (%s); trying a chat call anyway." % err)

    spec = PERSONAS["Hunter"]
    system = ("%s\n\n%s\n\nEach turn you get your situation. Reply with ONE JSON object only:\n"
              '{"intent": "<one of: %s>", "say": "<at most 12 words, in character>"}\nIntents: %s.'
              % (spec["voice"], RULES, "|".join(INTENTS), INTENT_HELP))
    user = summarize(SAMPLE, "Hunter", "hunt", ["Hunter lost a life (2 left) on level 2"])
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    rows = []
    for eff in a.efforts.split(","):
        eff = eff.strip()
        e.effort = None if eff == "default" else eff
        lats, costs, ins, outs, reas, ok = [], [], [], [], [], 0
        sample = None
        for _ in range(a.n):
            try:
                text, info = e.chat(msgs, role="check", max_tokens=600)
            except CapReached as err:
                sys.exit(str(err))
            except EmberError as err:
                print("  effort %-8s error: %s" % (eff, str(err)[:200]))
                break
            if eff != "default" and info["effort"] != eff:
                print("  effort %-8s not accepted by this model; it used %s instead" % (eff, info["effort"]))
            u = info["usage"]
            lats.append(info["latency_s"]); costs.append(info["cost"])
            ins.append(u.get("prompt_tokens", 0)); outs.append(u.get("completion_tokens", 0))
            reas.append(info.get("reasoning_tokens") or 0)
            d = extract_json(text) or {}
            ok += d.get("intent") in INTENTS
            sample = text.strip().replace("\n", " ")[:120]
        if lats:
            rows.append((eff, statistics.median(lats), statistics.mean(costs), statistics.mean(ins),
                         statistics.mean(outs), statistics.mean(reas), ok, len(lats), sample))
    print()
    print("%-8s %9s %11s %8s %8s %9s %7s" % ("effort", "latency", "$/decision", "in tok", "out tok", "reasoning", "valid"))
    for eff, lat, cost, i, o, r, ok, n, _ in rows:
        print("%-8s %8.2fs %11.6f %8d %8d %9d %4d/%d" % (eff, lat, cost, i, o, r, ok, n))
    for eff, *_, sample in rows:
        print("  %s reply: %s" % (eff, sample))
    if rows:
        best = min((r for r in rows if r[6]), key=lambda r: r[1], default=rows[0])
        per_min = 60 / 4.0 * best[2]      # one decision every 4 s per bot
        t = 36 * 6 * per_min + 6 * 6 * per_min
        print("\nFastest valid effort: %s (%.2fs). At one decision every 4 s per bot:" % (best[0], best[1]))
        print("  one bot-minute ~ $%.5f; a 36-bot round + 6-bot final, 6 min each ~ $%.3f (plus a few cents of commentary)"
              % (per_min, t))
        if best[1] > 3:
            print("  Ember is slower than 3 s here: use --period 6 or more so decisions don't pile up.")
        print("  Suggested: python3 royale.py ... --effort %s" % best[0])
    print("\nThis check spent $%.5f. Total in spend.json: $%.5f of the $%.2f cap."
          % (ledger.snapshot()["spent"] - before, ledger.snapshot()["spent"], ledger.cap))


if __name__ == "__main__":
    main()
