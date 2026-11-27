# Blaze Battle Royale

Dozens of AI bots, each with its own personality and each thinking with **Muse Ember**, race
through the same burning maze city in [Blaze](https://github.com/nine10go/blaze). A live
leaderboard tracks every bot, an Ember commentator calls the action, and a cost meter shows
the whole tournament running on a few cents of a $10 credit.

Built for the Muse Ember Hackathon (Cerebral Valley, Nov 23 to Dec 2, 2026).

## How it works

```
             every ~4 s, or right after a lost life / new level
   ┌──────────────┐  situation summary  ┌────────────────┐
   │  bot thread  │ ──────────────────► │   Muse Ember   │  "intent": hunt / gems / exit / safe / ...
   │  (strategist)│ ◄────────────────── │ (persona voice)│  "say": a line in character
   └──────┬───────┘   intent + taunt    └────────────────┘
          │ intent
   ┌──────▼───────┐  observe / move (MCP, ~10 calls/s)   ┌──────────────────┐
   │ fast planner │ ───────────────────────────────────► │  Blaze server    │  up to 6 bots per world
   │ (A*, in code)│ ◄─────────────────────────────────── │  (one per arena) │
   └──────────────┘          observation                 └──────────────────┘
```

- **Ember decides, code executes.** Blaze is real time: enemies move while a model thinks. So
  Ember chooses *what* the bot is trying to do (and talks trash), and a planner in code turns
  that into moves many times a second. If Ember is slow or down, the bot keeps playing on its
  last plan.
- **Ten personalities** (`personas.py`): Hunter, Coward, Gem Hoarder, Speedrunner, Explorer,
  Trickster, Tank, Gambler, Shadow, Diplomat. Each has its own Ember voice and planner weights.
- **Real multiplayer.** Bots share one city per arena. Every player's burning trail hurts the
  others, and only the gem leader sees the exit, so personalities collide.
- **Tournament** (`royale.py`): round 1 puts every bot into an arena at once (6 per arena,
  Blaze's limit for agents), then the best go through to a 6-bot final. Ranking: level, then
  score, then who survived longer.
- **Commentator**: every ~12 s Ember turns the latest events into a line of play-by-play.
- **Hard spending cap** (`ember.py`): every call reserves its worst-case cost before it is sent
  and is refused if it would cross the cap. Spend is kept in `spend.json`, shared by all runs,
  so the cap holds across tournaments. Default $6, and nothing can raise it above $9.

## Run it

Needs Python 3.9+ (standard library only), and a local Blaze server build.

```bash
# 1. Build the Blaze server once (in your blaze checkout)
cd ~/blaze && npm install && npm run build          # web assets for the spectate page
cd server && cargo build --release

# 2. Set your Meta Model API key (never written to any file)
export MODEL_API_KEY=LLM_...

# 3. Check the key, and measure Ember's speed and cost per decision (under one cent)
cd ~/royale && python3 ember_check.py

# 4. Run a tournament: 36 bots in 6 arenas, 6-minute heats, then a final
python3 royale.py --server-bin ~/blaze/server/target/release/blaze-server --blaze-root ~/blaze \
                  --bots 36 --minutes 6 --cap 3
# Leaderboard: http://127.0.0.1:8099/     Watch an arena: the "watch" links on the page
```

Useful flags: `--effort` (reasoning effort; `ember_check.py` suggests one), `--period` (seconds
between Ember calls per bot), `--no-ember` (rules-only baseline, no API calls),
`--arena <MCP URL>` (use servers you started yourself), `--linger 600` (keep the page up after
the final for recording).

Test without a key: `python3 mock_ember.py &` then
`EMBER_BASE_URL=http://127.0.0.1:8700/v1 MODEL_API_KEY=mock ROYALE_LEDGER=/tmp/mock.json python3 royale.py ...`

## Output

- `results/tournament-<time>.json`: every heat, placings, points, Ember spend
- `results/events-<time>.jsonl`: every event, intent switch, taunt and commentary line
- `web/live.json`: the live state the leaderboard page reads

## Cost

Muse Ember costs $0.10 per million input tokens and $0.50 per million output tokens.
Measured on Nov 27 with `ember_check.py` (520 input tokens per decision):

| reasoning effort | latency | $ per decision | valid replies |
|---|---|---|---|
| minimal | ~1.0 to 1.5 s | ~$0.0001 | 3/3 |
| low | ~2.3 s | ~$0.0003 | 3/3 |
| model default | ~1.6 s | ~$0.0003 | 0/3 (spent every token reasoning) |

`none` isn't accepted by Ember and falls back to `minimal`, which is the default here. At one
decision every 4 s per bot, a 36-bot round plus a 6-bot final (6 minutes each) costs about
$0.39 plus a few cents of commentary, well under the $6 cap.

## Files

`royale.py` tournament, arenas, commentator, live page · `bot.py` one bot (mover + Ember
strategist) · `planner.py` fast pathing · `personas.py` personalities · `ember.py` Ember client
and spending cap · `ember_check.py` key, speed and cost check · `mock_ember.py` offline stand-in
· `mcp_client.py` Blaze MCP client · `web/index.html` leaderboard.
