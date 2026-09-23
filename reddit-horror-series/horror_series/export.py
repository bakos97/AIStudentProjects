"""Write series to disk: one folder per story with JSON, a readable script and TTS-ready text files."""

from __future__ import annotations

import json
import re
from pathlib import Path

from .series import Series


def slugify(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "story"


def series_markdown(s: Series) -> str:
    total = sum(p.est_seconds for p in s.parts)
    lines = [
        f"# {s.series_title or s.source_title}",
        "",
        f"- Source: [{s.source_title}]({s.url}) by u/{s.author} on r/{s.subreddit} ({s.score} upvotes)",
        f"- Parts: {len(s.parts)} - total runtime ~{total / 60:.1f} min",
        f"- Fitness: {s.fitness.get('total')} "
        f"(cliffhanger avg {s.fitness.get('cliffhanger_avg')}, weakest {s.fitness.get('cliffhanger_min')})",
        "",
        "> Get the author's permission before publishing a narration, and credit them in every description.",
        "",
    ]
    for p in s.parts:
        lines += [
            f"## {p.title}",
            f"*~{p.est_seconds:.0f}s · {p.words} words · cliffhanger strength {p.cliffhanger_strength}"
            f" · thumbnail: \"{p.thumbnail_text}\"*",
            "",
            "### HOOK (0-3s)",
            f"**{p.hook}**",
            "",
        ]
        if p.hook_alternates:
            lines += ["Alternate hooks:", *[f"- {h}" for h in p.hook_alternates], ""]
        if p.recap:
            lines += ["### RECAP", p.recap, ""]
        lines += ["### STORY", p.story, "", "### CLIFFHANGER", f"**{p.cliffhanger}**", "",
                  "### CALL TO ACTION", p.call_to_action, "", "---", ""]
    return "\n".join(lines)


def write_series(s: Series, out_dir: Path) -> Path:
    folder = Path(out_dir) / f"{slugify(s.source_title)}-{s.story_id}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "series.json").write_text(json.dumps(s.to_dict(), indent=2, ensure_ascii=False))
    (folder / "series.md").write_text(series_markdown(s))
    for p in s.parts:
        (folder / f"part_{p.number:02d}.txt").write_text(p.script() + "\n")
    return folder


def write_leaderboard(series: list[Series], out_dir: Path) -> Path:
    out = Path(out_dir) / "leaderboard.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        "| # | Fitness | Story | Sub | Upvotes | Words | Avg cliff | Weakest cliff | Part-1 hook |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for i, s in enumerate(series, 1):
        f = s.fitness
        hook = s.parts[0].hook.replace("|", "/")
        hook = hook if len(hook) < 90 else hook[:87] + "..."
        rows.append(
            f"| {i} | {f['total']} | [{s.source_title.replace('|', '/')}]({s.url}) | r/{s.subreddit} | "
            f"{s.score} | {f['words']} | {f['cliffhanger_avg']} | {f['cliffhanger_min']} | {hook} |"
        )
    out.write_text("# Best stories for 10-part series\n\n" + "\n".join(rows) + "\n")
    return out
