"""Optional: let Claude punch up the hooks, recaps and titles.

The story text itself is never rewritten - only the packaging around it
(hook, recap, cliffhanger voice-over line, title, thumbnail text). The
heuristic split from ``series.py`` is kept, so the output stays faithful to
the original author's words.

Requires ``ANTHROPIC_API_KEY`` (or an ``ant auth login`` profile).
"""

from __future__ import annotations

import anthropic
from pydantic import BaseModel, Field

from .reddit import Story
from .series import Series

MODEL = "claude-opus-5"

SYSTEM = """You are a showrunner for a faceless horror-narration YouTube channel that serialises \
Reddit horror stories into multi-part videos. Every part follows the same structure:

1. HOOK - the first 3 seconds. It decides whether the viewer swipes away. It must be one or two \
short spoken sentences (max ~30 words), concrete and visceral, creating an open loop the viewer \
needs closed. Good hooks: a disturbing flash-forward line, a rule being broken, a detail that is \
"wrong", a direct question to the viewer. Never open with channel intros, "Hey guys", or the title. \
Never spoil anything that happens after the current part.
2. RECAP (parts 2+) - one sentence re-stating the previous cliffhanger so new viewers are caught up.
3. STORY - the author's own text (you do not rewrite it).
4. CLIFFHANGER - the part already ends at a tense moment. Write a short narrator voice-over line \
that sharpens the open question without answering it.
5. CALL TO ACTION - pushes the viewer to the next part.

Write in the story's own voice (usually first person). Plain spoken English, no emojis, no hashtags."""


class PartPackaging(BaseModel):
    number: int
    hook: str = Field(description="Spoken hook for the first 3 seconds, max ~30 words")
    hook_alternates: list[str] = Field(description="Two more alternative hooks to A/B test")
    recap: str = Field(description="Starts with 'Part N.' then one sentence re-stating the previous cliffhanger; empty for part 1")
    cliffhanger_voiceover: str = Field(description="Short line after the part's final sentence that heightens suspense")
    call_to_action: str
    title: str = Field(description="YouTube title, max 70 characters, include 'Part N'")
    thumbnail_text: str = Field(description="2-5 words for the thumbnail")


class SeriesPackaging(BaseModel):
    series_title: str
    parts: list[PartPackaging]


def _prompt(story: Story, series: Series) -> str:
    chunks = [
        f"Source: r/{story.subreddit} - \"{story.title}\" by u/{story.author}",
        f"The story has already been split into {len(series.parts)} parts. "
        "Write the packaging for every part.",
    ]
    for p in series.parts:
        chunks.append(
            f"<part number=\"{p.number}\">\n<story>\n{p.story}\n</story>\n"
            f"<ends_on>\n{p.cliffhanger}\n</ends_on>\n</part>"
        )
    return "\n\n".join(chunks)


def package_with_claude(story: Story, series: Series, client: anthropic.Anthropic | None = None) -> Series:
    client = client or anthropic.Anthropic()
    response = client.beta.messages.parse(
        model=MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        system=SYSTEM,
        messages=[{"role": "user", "content": _prompt(story, series)}],
        output_format=SeriesPackaging,
    )
    if response.stop_reason == "refusal" or response.parsed_output is None:
        print(f"[warn] Claude did not package {story.id} ({response.stop_reason}); keeping heuristic hooks")
        return series

    by_number = {p.number: p for p in response.parsed_output.parts}
    for part in series.parts:
        pkg = by_number.get(part.number)
        if not pkg:
            continue
        # Keep the heuristic hook as an extra alternate - handy for A/B tests.
        part.hook_alternates = [*pkg.hook_alternates, part.hook, *part.hook_alternates][:5]
        part.hook = pkg.hook
        part.recap = pkg.recap if part.number > 1 else ""
        if pkg.cliffhanger_voiceover and part.number < part.total:
            part.cliffhanger = f"{part.cliffhanger}\n\n{pkg.cliffhanger_voiceover}"
        part.call_to_action = pkg.call_to_action
        part.title = pkg.title
        part.thumbnail_text = pkg.thumbnail_text
        part.recount()
    series.series_title = response.parsed_output.series_title
    return series
