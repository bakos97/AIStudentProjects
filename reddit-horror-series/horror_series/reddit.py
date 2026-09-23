"""Fetch top horror posts from Reddit.

Two modes:
  * Public JSON endpoints (no credentials, rate limited, may be blocked on some networks).
  * OAuth "application only" mode when REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are set
    (create a "script" app at https://www.reddit.com/prefs/apps).
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Iterator

import requests

DEFAULT_SUBREDDITS = [
    "nosleep",
    "LetsNotMeet",
    "TrueScaryStories",
    "creepyencounters",
    "Thetruthishere",
    "stayawake",
]

USER_AGENT = os.environ.get(
    "REDDIT_USER_AGENT", "python:reddit-horror-series:0.1 (by /u/your_username)"
)


@dataclass
class Story:
    id: str
    subreddit: str
    title: str
    author: str
    score: int
    num_comments: int
    url: str
    over_18: bool
    created_utc: float
    text: str

    @property
    def word_count(self) -> int:
        return len(self.text.split())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Story":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------- cleanup

_EDIT_RE = re.compile(r"^\s*(edit|update|ps|p\.s\.|tl;?dr|x-?post|next part|\[?part \d+)\b.*$", re.I)
_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_URL_RE = re.compile(r"https?://\S+")


def clean_selftext(raw: str) -> str:
    """Convert Reddit markdown to plain narration text."""
    text = html.unescape(raw or "")
    text = text.replace("​", "").replace("&#x200B;", "")
    text = _LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub("", text)
    text = re.sub(r">!(.*?)!<", r"\1", text)  # spoilers
    text = re.sub(r"(\*\*|__|~~|`)", "", text)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text)  # *italics*
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]*", "", text, flags=re.M)  # headings
    text = re.sub(r"^[ \t]*>[ \t]?", "", text, flags=re.M)  # quotes
    text = re.sub(r"^[ \t]*([-*_][ \t]*){3,}$", "", text, flags=re.M)  # horizontal rules

    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", text)]
    paragraphs = [p for p in paragraphs if p]
    # Drop trailing author notes ("Edit: thanks for the gold", "Part 2 here", ...)
    while paragraphs and _EDIT_RE.match(paragraphs[-1]):
        paragraphs.pop()
    return "\n\n".join(paragraphs)


# --------------------------------------------------------------------------- client


class RedditClient:
    def __init__(self, session: requests.Session | None = None, sleep: float = 1.1):
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.sleep = sleep
        self.base = "https://www.reddit.com"
        cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
        if cid and secret:
            resp = self.session.post(
                "https://www.reddit.com/api/v1/access_token",
                auth=(cid, secret),
                data={"grant_type": "client_credentials"},
                timeout=30,
            )
            resp.raise_for_status()
            self.session.headers["Authorization"] = f"bearer {resp.json()['access_token']}"
            self.base = "https://oauth.reddit.com"

    def _get(self, path: str, params: dict) -> dict:
        for attempt in range(4):
            resp = self.session.get(f"{self.base}{path}", params=params, timeout=30)
            if resp.status_code == 429:
                time.sleep(2 ** (attempt + 2))
                continue
            resp.raise_for_status()
            time.sleep(self.sleep)
            return resp.json()
        resp.raise_for_status()
        return {}

    def top_posts(self, subreddit: str, time_filter: str = "all", limit: int = 100) -> Iterator[Story]:
        after = None
        fetched = 0
        while fetched < limit:
            params = {"t": time_filter, "limit": min(100, limit - fetched), "raw_json": 1}
            if after:
                params["after"] = after
            data = self._get(f"/r/{subreddit}/top.json", params)
            children = data.get("data", {}).get("children", [])
            if not children:
                return
            for child in children:
                fetched += 1
                story = post_to_story(child.get("data", {}))
                if story:
                    yield story
            after = data["data"].get("after")
            if not after:
                return


def post_to_story(post: dict) -> Story | None:
    if not post.get("is_self") or post.get("stickied"):
        return None
    raw = post.get("selftext") or ""
    if raw in ("[removed]", "[deleted]") or not raw.strip():
        return None
    return Story(
        id=post["id"],
        subreddit=post.get("subreddit", ""),
        title=html.unescape(post.get("title", "")).strip(),
        author=post.get("author") or "[deleted]",
        score=int(post.get("score", 0)),
        num_comments=int(post.get("num_comments", 0)),
        url="https://www.reddit.com" + post.get("permalink", ""),
        over_18=bool(post.get("over_18")),
        created_utc=float(post.get("created_utc", 0)),
        text=clean_selftext(raw),
    )


def scrape(subreddits: Iterable[str], time_filter: str, limit: int) -> list[Story]:
    client = RedditClient()
    stories: dict[str, Story] = {}
    for sub in subreddits:
        try:
            for story in client.top_posts(sub, time_filter, limit):
                stories[story.id] = story
        except requests.RequestException as exc:
            print(f"[warn] r/{sub}: {exc}")
    return list(stories.values())


def save_stories(stories: list[Story], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([s.to_dict() for s in stories], indent=2, ensure_ascii=False))


def load_stories(path: Path) -> list[Story]:
    data = json.loads(Path(path).read_text())
    # Accept either our own dump or a raw Reddit listing JSON.
    if isinstance(data, dict) and "data" in data:
        return [s for c in data["data"]["children"] if (s := post_to_story(c["data"]))]
    return [Story.from_dict(d) for d in data]
