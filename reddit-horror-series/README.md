# Reddit Horror Series

Scrapes the top horror stories on Reddit, finds the ones that work as a **10-part YouTube series**, and writes a narration script for every part in the same format:

```
HOOK         0-3 s. A disturbing line that stops the scroll. The most important part.
(RECAP)      Part 2 onward: "Part 4. Last time: ..." so new viewers catch up.
STORY        The author's own words, unedited.
CLIFFHANGER  The part always stops right before something resolves.
CTA          "Part 5 is up next. Follow so you don't miss it."
```

## How it works

1. **Scrape** (`horror_series/reddit.py`) pulls top posts from r/nosleep, r/LetsNotMeet, r/TrueScaryStories, r/creepyencounters, r/Thetruthishere and r/stayawake, then converts the Reddit markdown into clean narration text. It removes links, formatting and trailing "Edit: thanks for the gold" notes.
2. **Filter** drops posts that can't become a good 10-parter:
   - posts that are already a series ("Part 2", "Update")
   - stories that are too short or too long for the chosen episode length
   - link posts and removed posts
3. **Split** (`horror_series/series.py`) uses dynamic programming to pick the 9 cut points. Each cut is scored on two things:
   - **cliffhanger strength**: how tense the last line is (dread words, a question, a short punchy line, unanswered dialogue), and whether the next sentence is a reveal or a turn ("Then I heard…")
   - **length balance**: episodes should be about the same length
4. **Hook**, which gets the most attention:
   - **Part 1** gets a *cold open*. It flash-forwards to the most terrifying standalone line from the middle of the story (never from the finale, so the ending stays unspoiled), then continues with "…But to understand how it got to that point, I have to start at the beginning."
   - **Parts 2-10** open with the scariest line from the first part of that episode, which previews what's coming. If nothing in that section is scary enough, the episode opens on the previous cliffhanger.
   - Every part also gets alternate hooks for A/B testing.
   - Hook lines are scored on dread vocabulary, length (5-18 words is ideal), whether they make sense without context, whether they address the viewer ("you"), and concrete details.
5. **Rank** gives each story a fitness score based on:
   - average and weakest cliffhanger
   - best hook line
   - Reddit upvotes
   - how many episodes land in the target length

   All candidates are written to `leaderboard.md`.
6. **Optional: Claude packaging** (`--llm`). Claude rewrites the hook, alternate hooks, recap, a cliffhanger voice-over line, the YouTube title and the thumbnail text for all 10 parts in one structured-output call. The story text itself is never rewritten.

## Setup

```bash
cd reddit-horror-series
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Reddit increasingly blocks anonymous JSON requests. If `scrape` returns 403/429, create a free "script" app at <https://www.reddit.com/prefs/apps> and export:

```bash
export REDDIT_CLIENT_ID=...
export REDDIT_CLIENT_SECRET=...
export REDDIT_USER_AGENT="python:reddit-horror-series:0.1 (by /u/<your reddit name>)"
```

For `--llm` you also need `ANTHROPIC_API_KEY`.

## Usage

```bash
# 1. Download the top posts of all time (200 per subreddit)
python -m horror_series scrape --time all --limit 200

# 2. Rank them and write the 5 best as 10-part Shorts series
python -m horror_series build --top 5

# ...with Claude writing the hooks/titles
python -m horror_series build --top 5 --llm

# Scrape + build in one go, longer episodes (3-12 min each)
python -m horror_series run --time year --preset long --top 3
```

Useful flags: `--parts 10`, `--preset shorts|long`, `--wpm 160` (narration speed), `--no-nsfw`, `--subreddits nosleep LetsNotMeet`, `-v` (explains why stories were skipped).

`build --stories` also accepts a raw Reddit listing JSON. For example, save `https://www.reddit.com/r/nosleep/top.json?t=all&limit=100` in your browser and pass that file.

| Preset | Story narration per part | Total story length that fits |
|---|---|---|
| `shorts` (default) | 40-160 s (Shorts/TikTok/Reels, under 3 min with hook and CTA) | ~1,070-4,270 words |
| `long` | 3-12 min | ~4,800-19,200 words |

## Output

```
output/
  leaderboard.md                              # every story that fits, ranked
  i-worked-the-night-shift-at-a-abc123/
    series.md                                 # readable production script with hooks, alternates, timings
    series.json                               # everything, for automation (TTS, video editor, uploader)
    part_01.txt ... part_10.txt               # plain narration text, ready for a TTS engine
```

Example from `series.md`:

```
## I worked the night shift at a storage facility (Part 6/10)
*~59s · 158 words · cliffhanger strength 1.75*

### HOOK (0-3s)
**On the night of week nine, the booth phone rang at 3:05 AM.**

### RECAP
Part 6. Last time: The last time, his nose was almost touching it, and I could see that
his teeth were too small for his mouth, like baby teeth in a grown man's face.

### STORY
...

### CLIFFHANGER
**That's what I kept saying. Nothing has actually happened to me.**

### CALL TO ACTION
Part 7 is up next. Follow so you don't miss it.
```

## Tests

```bash
python -m pytest -q
```

The tests run offline against an original sample story (`tests/fixtures/the_night_shift.txt`).

## Permission and credit

Reddit stories are copyrighted by their authors. r/nosleep in particular requires **explicit permission from the author** before a story is narrated or monetised. Before you publish:

- Message the author.
- Credit them (`u/author` plus a link) in every part's description. `series.md` has the source link at the top.
- Follow YouTube's policies on reused content.
