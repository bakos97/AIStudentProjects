"""Split a story into N parts, each shaped as HOOK -> STORY -> CLIFFHANGER (+ call to action).

The split is chosen with dynamic programming: it balances part length against how
strong the cliffhanger at every cut is, so each part ends right before something
the viewer needs to see resolved.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from .reddit import Story
from .text import (
    Sentence,
    WORDS_PER_MINUTE,
    cliffhanger_score,
    hook_score,
    seconds_for,
    split_sentences,
)

PRESETS = {
    # Narration seconds per part (story body only; hook + CTA add ~10s).
    "shorts": (40, 160),   # YouTube Shorts / TikTok / Reels (<= 3 min total)
    "long": (180, 720),    # regular YouTube episodes, 3-12 min
}

# Below this hook_score a teaser line is too mundane to open an episode with.
MIN_TEASER_SCORE = 3.5

SERIES_TITLE_RE = re.compile(r"\b(part|pt\.?|chapter|update|final|finale)\s*\d*\b|\(\d+/\d+\)", re.I)


@dataclass
class Part:
    number: int
    total: int
    hook: str
    hook_alternates: list[str]
    recap: str
    story: str
    cliffhanger: str
    call_to_action: str
    title: str
    words: int
    est_seconds: float
    cliffhanger_strength: float
    thumbnail_text: str = ""

    def recount(self, wpm: int = WORDS_PER_MINUTE) -> None:
        self.words = len(self.script().split())
        self.est_seconds = round(seconds_for(self.words, wpm), 1)

    def script(self) -> str:
        """Plain narration script, ready for a TTS engine or voice actor."""
        blocks = [self.hook]
        if self.recap:
            blocks.append(self.recap)
        blocks += [self.story, self.cliffhanger, self.call_to_action]
        return "\n\n".join(b for b in blocks if b)


@dataclass
class Series:
    story_id: str
    source_title: str
    subreddit: str
    author: str
    url: str
    score: int
    series_title: str = ""
    parts: list[Part] = field(default_factory=list)
    fitness: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- splitting


def choose_cuts(sents: list[Sentence], n_parts: int, length_weight: float = 6.0) -> list[int]:
    """Return indices of the last sentence of each part (len == n_parts)."""
    n = len(sents)
    if n < n_parts:
        raise ValueError(f"story has {n} sentences, need at least {n_parts}")
    prefix = [0]
    for s in sents:
        prefix.append(prefix[-1] + s.words)
    total = prefix[-1]
    target = total / n_parts
    lo, hi = 0.55 * target, 1.6 * target
    cliff = [cliffhanger_score(sents, i) for i in range(n)]

    def part_cost(a: int, b: int) -> float | None:  # sentences a..b inclusive
        words = prefix[b + 1] - prefix[a]
        if not lo <= words <= hi:
            return None
        return -length_weight * ((words - target) / target) ** 2

    NEG = -math.inf
    dp = [[NEG] * n for _ in range(n_parts + 1)]
    back = [[-1] * n for _ in range(n_parts + 1)]
    for j in range(n):
        c = part_cost(0, j)
        if c is not None:
            dp[1][j] = c + (cliff[j] if n_parts > 1 else 0.0)
    for k in range(2, n_parts + 1):
        for j in range(k - 1, n):
            bonus = cliff[j] if k < n_parts else 0.0
            best, arg = NEG, -1
            # scan previous end i (part k spans i+1..j); stop once the part is too long
            for i in range(j - 1, k - 3, -1):
                if dp[k - 1][i] == NEG:
                    if prefix[j + 1] - prefix[i + 1] > hi:
                        break
                    continue
                c = part_cost(i + 1, j)
                if c is None:
                    if prefix[j + 1] - prefix[i + 1] > hi:
                        break
                    continue
                v = dp[k - 1][i] + c + bonus
                if v > best:
                    best, arg = v, i
            dp[k][j], back[k][j] = best, arg
    if dp[n_parts][n - 1] == NEG:
        # Fall back to plain equal-length cuts at sentence boundaries.
        return [
            min(range(n), key=lambda j: abs(prefix[j + 1] - target * (k + 1)))
            for k in range(n_parts - 1)
        ] + [n - 1]
    cuts, j = [], n - 1
    for k in range(n_parts, 0, -1):
        cuts.append(j)
        j = back[k][j]
    return cuts[::-1]


# --------------------------------------------------------------------------- hooks


def _best_hooks(sents: list[Sentence], idxs: range | list[int], exclude: set[int], k: int = 3) -> list[str]:
    ranked = sorted((i for i in idxs if i not in exclude), key=lambda i: hook_score(sents[i]), reverse=True)
    out: list[str] = []
    for i in ranked:
        txt = sents[i].text.strip()
        if txt not in out:
            out.append(txt)
        if len(out) == k:
            break
    return out


def _cliffhanger_span(sents: list[Sentence], start: int, end: int) -> int:
    """First sentence index of the cliffhanger at the end of a part (1-2 sentences)."""
    if end - 1 >= start and sents[end].words <= 8 and sents[end - 1].paragraph == sents[end].paragraph:
        return end - 1  # keep a short punch line together with its set-up
    return end


def build_series(story: Story, n_parts: int = 10, preset: str = "shorts",
                 wpm: int = WORDS_PER_MINUTE) -> Series:
    sents = split_sentences(story.text)
    cuts = choose_cuts(sents, n_parts)
    starts = [0] + [c + 1 for c in cuts[:-1]]
    finale_start = starts[-1]
    series = Series(story.id, story.title, story.subreddit, story.author, story.url, story.score,
                    series_title=_short_title(story.title))

    prev_cliff = ""
    for k, (a, b) in enumerate(zip(starts, cuts), start=1):
        is_last = k == n_parts
        # The finale's "cliffhanger" is the ending itself: its last line gets its own beat.
        cliff_from = b if is_last else _cliffhanger_span(sents, a, b)
        body = _join(sents[a:cliff_from])
        cliff_text = _join(sents[cliff_from:b + 1])

        if k == 1:
            # Cold open: flash-forward to a terrifying line from later in the story
            # (never from the finale, so nothing is spoiled).
            teasers = _best_hooks(sents, range(starts[1], finale_start), exclude=set())
            hooks = [f"{t} ... But to understand how it got to that point, I have to start at the beginning."
                     for t in teasers]
            hooks.append(_title_hook(story))
            recap = ""
        else:
            span = b - a + 1
            # Tease a line from early in this part so the cliffhanger (or ending) stays hidden.
            window = range(a, a + max(1, int(span * (0.5 if is_last else 0.7))))
            idxs = [i for i in window if i < cliff_from]
            teasers = _best_hooks(sents, idxs, exclude=set())
            best = max((hook_score(sents[i]) for i in idxs), default=0.0)
            if best >= MIN_TEASER_SCORE:
                hooks = teasers + [prev_cliff]
                recap = f"Part {k}. Last time: {prev_cliff}"
            else:
                # Nothing scary enough to tease: open on the unresolved cliffhanger instead.
                hooks = [prev_cliff] + teasers
                recap = f"Part {k}."

        cta = (f"Part {k + 1} is up next. Follow so you don't miss it."
               if not is_last else
               f"That was the final part. Story by u/{story.author} on r/{story.subreddit}.")
        part = Part(
            number=k,
            total=n_parts,
            hook=hooks[0],
            hook_alternates=hooks[1:],
            recap=recap,
            story=body,
            cliffhanger=cliff_text,
            call_to_action=cta,
            title=f"{_short_title(story.title)} (Part {k}/{n_parts})",
            words=0,
            est_seconds=0.0,
            cliffhanger_strength=round(cliffhanger_score(sents, b), 2) if not is_last else 0.0,
            thumbnail_text=_short_title(story.title, 30),
        )
        part.recount(wpm)
        series.parts.append(part)
        prev_cliff = cliff_text

    series.fitness = score_fitness(story, series, sents, preset, wpm)
    return series


def _title_hook(story: Story) -> str:
    return f"This story was posted on r/{story.subreddit}, and the author swears it's real. It's called: {story.title}."


def _short_title(title: str, max_len: int = 60) -> str:
    title = SERIES_TITLE_RE.sub("", title).strip(" -:|[]()")
    return title if len(title) <= max_len else title[: max_len - 1].rsplit(" ", 1)[0] + "…"


def _join(sents: list[Sentence]) -> str:
    out, para = [], None
    for s in sents:
        if para is not None and s.paragraph != para:
            out.append("\n\n")
        elif out:
            out.append(" ")
        out.append(s.text)
        para = s.paragraph
    return "".join(out)


# --------------------------------------------------------------------------- ranking


def is_candidate(story: Story, n_parts: int = 10, preset: str = "shorts",
                 wpm: int = WORDS_PER_MINUTE, allow_nsfw: bool = True) -> tuple[bool, str]:
    """Cheap pre-filter before the (more expensive) split."""
    lo_s, hi_s = PRESETS[preset]
    lo_w, hi_w = lo_s * wpm / 60 * n_parts, hi_s * wpm / 60 * n_parts
    if SERIES_TITLE_RE.search(story.title):
        return False, "already a multi-part series / update"
    if not allow_nsfw and story.over_18:
        return False, "nsfw"
    if story.word_count < lo_w:
        return False, f"too short ({story.word_count} words < {lo_w:.0f})"
    if story.word_count > hi_w:
        return False, f"too long ({story.word_count} words > {hi_w:.0f})"
    if len(split_sentences(story.text)) < n_parts * 4:
        return False, "too few sentences"
    return True, "ok"


def score_fitness(story: Story, series: Series, sents: list[Sentence], preset: str, wpm: int) -> dict:
    lo_s, hi_s = PRESETS[preset]
    cliffs = [p.cliffhanger_strength for p in series.parts[:-1]]
    hook_top = max((hook_score(s) for s in sents), default=0.0)
    in_range = sum(lo_s <= seconds_for(len(p.story.split()), wpm) <= hi_s for p in series.parts) / len(series.parts)
    cliff_avg = sum(cliffs) / len(cliffs) if cliffs else 0.0
    cliff_min = min(cliffs) if cliffs else 0.0
    popularity = math.log10(max(story.score, 1))
    total = (
        2.0 * cliff_avg          # every episode must end on tension
        + 1.0 * cliff_min        # ...including the weakest one
        + 1.5 * hook_top         # a killer cold open for part 1
        + 1.5 * popularity       # proven with Reddit readers
        + 4.0 * in_range         # episode lengths fit the format
    )
    return {
        "total": round(total, 2),
        "cliffhanger_avg": round(cliff_avg, 2),
        "cliffhanger_min": round(cliff_min, 2),
        "best_hook_line": round(hook_top, 2),
        "popularity_log10": round(popularity, 2),
        "parts_in_length_range": round(in_range, 2),
        "words": story.word_count,
    }


def rank_stories(stories: list[Story], n_parts: int = 10, preset: str = "shorts",
                 wpm: int = WORDS_PER_MINUTE, allow_nsfw: bool = True,
                 verbose: bool = False) -> list[Series]:
    built: list[Series] = []
    for story in stories:
        ok, why = is_candidate(story, n_parts, preset, wpm, allow_nsfw)
        if not ok:
            if verbose:
                print(f"  skip {story.id} {story.title[:50]!r}: {why}")
            continue
        try:
            built.append(build_series(story, n_parts, preset, wpm))
        except ValueError as exc:
            if verbose:
                print(f"  skip {story.id}: {exc}")
    built.sort(key=lambda s: s.fitness["total"], reverse=True)
    return built
