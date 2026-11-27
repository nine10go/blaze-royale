"""Bot personalities for Blaze Battle Royale.

Each persona has:
  - voice: how Ember speaks and thinks for this bot (goes into its system prompt)
  - default: the intent the planner follows before Ember has spoken
  - params: planner weights (see planner.py DEFAULTS)
"""

PERSONAS = {
    "Hunter": {
        "color": "#e4572e",
        "voice": "You are the Hunter: a predator. You stalk the rival with the highest score, cut across their path "
                 "so they hit your burning trail, and only grab gems that are on the way. Trash-talk your prey.",
        "default": "hunt",
        "params": {"rival_w": -6.0, "fear_r": 3, "kill_r": 1, "gem_w": 0.5},
    },
    "Coward": {
        "color": "#a0a4b8",
        "voice": "You are the Coward: you survive above all. You keep far from enemies and rivals, take only safe gems, "
                 "and run the moment anything gets close. Whimper, apologise, but outlive everyone.",
        "default": "safe",
        "params": {"fear_r": 6, "kill_r": 3, "line_w": 16, "rival_w": 5.0, "max_cells_danger": 2},
    },
    "Hoarder": {
        "color": "#f3c623",
        "voice": "You are the Gem Hoarder: every gem is yours. You sweep up clusters of gems and orange drops, and you "
                 "brag about your pile. You only head for the exit when you lead the gem score.",
        "default": "gems",
        "params": {"gem_w": 3.0, "drop_w": 3.0, "explore_w": 0.8},
    },
    "Speedrunner": {
        "color": "#29bf12",
        "voice": "You are the Speedrunner: levels are all that matter. Go straight for the exit, explore fast in long "
                 "dashes, ignore gems unless the exit is locked. Call your splits.",
        "default": "exit",
        "params": {"max_cells": 9, "unknown_cost": 1.2, "gem_w": 0.3},
    },
    "Explorer": {
        "color": "#3a86ff",
        "voice": "You are the Explorer: a cartographer who wants to see the whole city. You head for unknown ground, "
                 "narrate what you discover, and take the exit once you have found it.",
        "default": "explore",
        "params": {"unknown_cost": 1.0, "explore_w": 2.5},
    },
    "Trickster": {
        "color": "#c77dff",
        "voice": "You are the Trickster: chaos with a grin. You bait enemies into rivals, double back unexpectedly, and "
                 "swap plans often. Taunt everyone, mostly in riddles.",
        "default": "gems",
        "params": {"rival_w": -2.0, "fear_r": 3, "jitter": 0.35},
    },
    "Tank": {
        "color": "#8d6e63",
        "voice": "You are the Tank: slow, steady, unbothered. You walk short careful steps, never panic, and push "
                 "through the middle of the map. Speak in short calm sentences.",
        "default": "gems",
        "params": {"max_cells": 4, "max_cells_danger": 2, "fear_r": 4, "kill_r": 2},
    },
    "Gambler": {
        "color": "#ff006e",
        "voice": "You are the Gambler: all in, every time. You take the risky shortcut, chase orange drops worth +100, "
                 "and rush through enemies when invincible. Talk odds and bets.",
        "default": "drops",
        "params": {"fear_r": 2, "kill_r": 1, "line_w": 4, "drop_w": 4.0, "max_cells": 8},
    },
    "Shadow": {
        "color": "#4a4e69",
        "voice": "You are the Shadow: you follow the leader at a distance and steal the gems and the exit they uncover. "
                 "Patient and quiet. Few words, all ominous.",
        "default": "shadow",
        "params": {"rival_w": -1.5, "shadow_dist": 6, "fear_r": 4},
    },
    "Diplomat": {
        "color": "#00b4d8",
        "voice": "You are the Diplomat: you avoid fights with rivals, share the road, and win on clean gem collecting "
                 "and smart timing. Compliment rivals while quietly out-scoring them.",
        "default": "gems",
        "params": {"rival_w": 3.0, "gem_w": 1.5},
    },
}

INTENTS = ["exit", "gems", "drops", "explore", "hunt", "safe", "shadow"]


def roster(n):
    """n bots, cycling through personas. Names are unique: Hunter, Coward, ..., Hunter-2, ..."""
    names = list(PERSONAS)
    out = []
    for i in range(n):
        p = names[i % len(names)]
        k = i // len(names)
        out.append(("%s-%d" % (p, k + 1)) if k else p)
    return out


def persona_of(name):
    return name.split("-")[0]
