"""Command line interface.

  python -m horror_series scrape --time year --limit 200         # Reddit -> data/stories.json
  python -m horror_series build  --top 5 [--llm]                 # stories -> output/<series>/
  python -m horror_series run    --time all --top 5 [--llm]      # both in one go
"""

from __future__ import annotations

import argparse
from pathlib import Path

from .export import write_leaderboard, write_series
from .reddit import DEFAULT_SUBREDDITS, load_stories, save_stories, scrape
from .series import PRESETS, rank_stories
from .text import WORDS_PER_MINUTE


def _add_scrape_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--subreddits", nargs="+", default=DEFAULT_SUBREDDITS)
    p.add_argument("--time", default="all", choices=["hour", "day", "week", "month", "year", "all"])
    p.add_argument("--limit", type=int, default=200, help="posts to fetch per subreddit (max ~1000)")
    p.add_argument("--stories", type=Path, default=Path("data/stories.json"))


def _add_build_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stories", type=Path, default=Path("data/stories.json"),
                   help="our dump from `scrape` or a raw Reddit listing JSON")
    p.add_argument("--out", type=Path, default=Path("output"))
    p.add_argument("--top", type=int, default=5, help="how many series to write")
    p.add_argument("--parts", type=int, default=10)
    p.add_argument("--preset", choices=sorted(PRESETS), default="shorts")
    p.add_argument("--wpm", type=int, default=WORDS_PER_MINUTE, help="narration words per minute")
    p.add_argument("--no-nsfw", action="store_true", help="skip posts marked NSFW")
    p.add_argument("--llm", action="store_true", help="let Claude write hooks/recaps/titles")
    p.add_argument("-v", "--verbose", action="store_true")


def cmd_scrape(args) -> int:
    stories = scrape(args.subreddits, args.time, args.limit)
    save_stories(stories, args.stories)
    print(f"saved {len(stories)} stories -> {args.stories}")
    return 0 if stories else 1


def cmd_build(args) -> int:
    stories = load_stories(args.stories)
    print(f"loaded {len(stories)} stories")
    ranked = rank_stories(stories, args.parts, args.preset, args.wpm,
                          allow_nsfw=not args.no_nsfw, verbose=args.verbose)
    print(f"{len(ranked)} stories fit a {args.parts}-part '{args.preset}' series")
    if not ranked:
        return 1
    chosen = ranked[: args.top]
    if args.llm:
        from .llm import package_with_claude

        by_id = {s.id: s for s in stories}
        for s in chosen:
            print(f"  packaging with Claude: {s.source_title[:60]}")
            package_with_claude(by_id[s.story_id], s)
    for s in chosen:
        folder = write_series(s, args.out)
        print(f"  [{s.fitness['total']:6.2f}] {s.source_title[:60]:60} -> {folder}")
    print(f"leaderboard -> {write_leaderboard(ranked, args.out)}")
    return 0


def cmd_run(args) -> int:
    rc = cmd_scrape(args)
    return rc if rc else cmd_build(args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="horror_series", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_scrape = sub.add_parser("scrape", help="download top posts from horror subreddits")
    _add_scrape_args(p_scrape)
    p_scrape.set_defaults(func=cmd_scrape)
    p_build = sub.add_parser("build", help="rank stories and write 10-part series scripts")
    _add_build_args(p_build)
    p_build.set_defaults(func=cmd_build)
    p_run = sub.add_parser("run", help="scrape + build")
    _add_build_args(p_run)
    for action in p_scrape._actions:  # reuse scrape flags, --stories is shared
        if action.dest not in ("help", "stories"):
            p_run._add_action(action)
    p_run.set_defaults(func=cmd_run)
    args = parser.parse_args(argv)
    return args.func(args)
