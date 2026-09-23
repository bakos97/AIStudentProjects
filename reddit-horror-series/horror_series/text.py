"""Sentence splitting and the heuristics that decide what makes a good hook or cliffhanger."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

WORDS_PER_MINUTE = 160  # typical TTS / narration pace

# Words that carry dread. Weighted: strong hits count more.
DREAD_STRONG = {
    "blood", "bloody", "scream", "screamed", "screaming", "corpse", "dead", "body", "bodies",
    "killed", "kill", "murder", "teeth", "grinning", "grin", "smiling", "whisper", "whispered",
    "whispering", "breathing", "footsteps", "knock", "knocking", "knocked", "shadow", "shadows",
    "staring", "stared", "watching", "behind", "basement", "attic", "closet", "mirror",
    "face", "eyes", "hollow", "crawling", "crawled", "twitching", "wrong", "missing", "vanished",
    "gone", "empty", "silence", "silent", "froze", "frozen", "terrified", "panic", "trapped",
    "locked", "unlocked", "handle", "doorknob", "voice", "name", "skin", "bones", "rotting",
    "smell", "cold", "dark", "darkness", "thing", "someone", "something", "nobody",
}
DREAD_MEDIUM = {
    "door", "window", "night", "phone", "rang", "call", "camera", "footage", "photo", "note",
    "watched", "woods", "forest", "hallway", "stairs", "bed", "sleep", "woke", "awake", "3am", "3:00",
    "moved", "moving", "standing", "stood", "outside", "inside", "alone", "again", "still",
    "never", "shouldn't", "couldn't", "wasn't", "didn't", "wouldn't", "last", "only",
}
# Openers that make a sentence depend on earlier context (bad for a standalone hook).
CONTEXT_OPENERS = {"this", "that", "these", "those", "he", "she", "they", "then", "so", "and",
                   "but", "also", "which", "anyway", "after", "later"}

_SENT_END_RE = re.compile(r"[.!?…]+[\"”’')\]]*(?=\s+[\"“‘(\[]?[A-Z0-9])")
_ABBREV = {"mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "mt", "ft", "no", "officer", "sgt", "lt"}
_DIALOGUE_TAG_RE = re.compile(
    r"\s+(I|he|she|they|we|it|[A-Z][a-z]+)\s+(said|asked|whispered|yelled|screamed|replied|shouted|muttered|called)\b"
)
_WORD_RE = re.compile(r"[a-z0-9:']+")


@dataclass
class Sentence:
    text: str
    paragraph: int
    ends_paragraph: bool

    @property
    def words(self) -> int:
        return len(self.text.split())


def _split_paragraph(para: str) -> list[str]:
    parts, start = [], 0
    for m in _SENT_END_RE.finditer(para):
        before = para[start:m.start()].split()
        if m.group().startswith(".") and before and before[-1].lower().strip("(\"“") in _ABBREV:
            continue  # "Mr. Smith"
        if re.search(r"[?!][\"”’]$", m.group()) and _DIALOGUE_TAG_RE.match(para, m.end()):
            continue  # "Why?" I asked him.
        parts.append(para[start:m.end()].strip())
        start = m.end()
    parts.append(para[start:].strip())
    return [p for p in parts if p]


def split_sentences(text: str) -> list[Sentence]:
    out: list[Sentence] = []
    for p_idx, para in enumerate(p for p in text.split("\n\n") if p.strip()):
        parts = _split_paragraph(para.strip())
        for i, s in enumerate(parts):
            out.append(Sentence(s, p_idx, i == len(parts) - 1))
    return out


def tokens(s: str) -> list[str]:
    return _WORD_RE.findall(s.lower().replace("’", "'"))


def dread(s: str) -> float:
    """How much fear vocabulary a sentence carries (0..~3)."""
    toks = tokens(s)
    if not toks:
        return 0.0
    hits = sum(2.0 for t in toks if t in DREAD_STRONG) + sum(1.0 for t in toks if t in DREAD_MEDIUM)
    # Diminishing returns + mild normalisation by length so long sentences don't win by default.
    return math.log1p(hits) * (1.0 if len(toks) < 25 else 25 / len(toks))


def is_dialogue(s: str) -> bool:
    return bool(re.search(r"[\"“”]", s))


_TURN_RE = re.compile(
    r"[\"“]?(then|suddenly|that's when|that was when|and then|until|on the (night|morning|day)|"
    r"in (week|the) |the next|when i (woke|got|looked|opened|turned)|nothing|i (heard|saw|felt|realized))\b",
    re.I,
)


def cliffhanger_score(sents: list[Sentence], i: int) -> float:
    """Score for ending a part right after sentence ``i``.

    Rewards ending on tension (a question, a short punchy line, dread words, an
    unanswered line of dialogue) right before the story would resolve it.
    Penalises ending mid-thought.
    """
    s = sents[i]
    t = s.text.rstrip("\"”’') ")
    d = dread(s.text)
    score = d * 1.5
    if s.words <= 8:
        score += 1.0 * min(d, 1.0)  # punchy - but only if it actually carries tension
    elif s.words > 35:
        score -= 0.8
    if t.endswith("?"):
        score += 1.0
    if t.endswith(("...", "…", "—", "-")):
        score += 1.2
    if t.endswith("!"):
        score += 0.4
    if t.endswith(":") or t.endswith(","):
        score -= 3.0
    if is_dialogue(s.text):
        score += 0.4
    if s.ends_paragraph:
        score += 0.6  # natural pause for the narrator
    if i + 1 < len(sents):
        nxt = sents[i + 1]
        first = (tokens(nxt.text) or [""])[0]
        if nxt.paragraph == s.paragraph and first in {"which", "and", "or"}:
            score -= 1.5  # cutting mid-thought
        # Cutting right before a reveal or a turn is what makes people click "next part".
        score += 0.5 * dread(nxt.text)
        if _TURN_RE.match(nxt.text):
            score += 0.8
    # Build-up: tension in the preceding sentences.
    if i >= 1:
        score += 0.3 * dread(sents[i - 1].text)
    return score


def hook_score(s: Sentence) -> float:
    """How well a single sentence works as a cold-open line in the first 3 seconds."""
    toks = tokens(s.text)
    if not toks:
        return -9.0
    d = dread(s.text)
    score = d * 2.0
    # A short line is only a good hook when it is also scary.
    punch = 0.3 + 0.7 * min(d, 1.5) / 1.5
    n = s.words
    if 5 <= n <= 18:
        score += 1.5 * punch
    elif n <= 25:
        score += 0.5 * punch
    else:
        score -= (n - 25) * 0.1 + 1.0
    if toks[0] in CONTEXT_OPENERS:
        score -= 1.2
    if toks[0] in {"never", "don't", "if", "nobody", "you", "every", "someone", "something"}:
        score += 0.4
    if "you" in toks or "your" in toks:
        score += 0.5
    if s.text.rstrip("\"”’') ").endswith("?"):
        score += 0.6
    if is_dialogue(s.text):
        score += 0.5
    if re.search(r"\b(19|20)\d\d\b|\b\d+ (years|days|nights|weeks)\b", s.text):
        score += 0.3  # concrete, real-feeling detail
    return score


def seconds_for(words: int, wpm: int = WORDS_PER_MINUTE) -> float:
    return words / wpm * 60
