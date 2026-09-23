"""Fetch top horror posts from Reddit (see the "sources" section below for how)."""

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


# --------------------------------------------------------------------------- sources
#
# Reddit blocked unauthenticated ``.json`` endpoints in May 2026 (403), and new API
# keys need manual approval under the Responsible Builder Policy. So there are three
# sources, tried in this order by ``--source auto``:
#
#   reddit-api  Official OAuth API. Only if REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are set
#               (an approved app). Real "top" ranking and live scores.
#   arctic      Arctic Shift (https://arctic-shift.photon-reddit.com), a free public Reddit
#               archive, no key. No "top" sort, so we scan the subreddit's post
#               metadata, rank by score ourselves, then fetch full text for the winners.
#   rss         Reddit's public RSS feed of /top. Full post text, but no score and
#               at most ~100 posts per subreddit. Rate limited to about 1 request/min.

ARCTIC_BASE = "https://arctic-shift.photon-reddit.com/api"
TIME_WINDOWS = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 31 * 86400, "year": 366 * 86400}


def _session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def _get(session: requests.Session, url: str, params: dict | None = None, sleep: float = 0.0,
         retries: int = 5) -> requests.Response:
    """GET with polite back-off on 429 / 5xx (honours Retry-After and X-RateLimit-Reset)."""
    for attempt in range(retries):
        resp = session.get(url, params=params, timeout=60)
        if resp.status_code == 429 or resp.status_code >= 500:
            wait = resp.headers.get("Retry-After") or resp.headers.get("X-RateLimit-Reset")
            try:
                delay = float(wait)
                # X-RateLimit-Reset may be an epoch timestamp rather than a number of seconds.
                delay = delay - time.time() if delay > 1e9 else delay
            except (TypeError, ValueError):
                delay = 2 ** (attempt + 2)
            time.sleep(min(max(delay, 1.0), 120))
            continue
        resp.raise_for_status()
        if sleep:
            time.sleep(sleep)
        return resp
    resp.raise_for_status()
    return resp


class RedditAPISource:
    """Official OAuth API ("application only" / read-only). Needs an approved app."""

    name = "reddit-api"

    def __init__(self, session: requests.Session | None = None, sleep: float = 0.7):
        cid, secret = os.environ.get("REDDIT_CLIENT_ID"), os.environ.get("REDDIT_CLIENT_SECRET")
        if not (cid and secret):
            raise RuntimeError("REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET are not set")
        self.session = session or _session()
        self.sleep = sleep
        resp = self.session.post(
            "https://www.reddit.com/api/v1/access_token",
            auth=(cid, secret),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        resp.raise_for_status()
        self.session.headers["Authorization"] = f"bearer {resp.json()['access_token']}"

    def top_posts(self, subreddit: str, time_filter: str = "all", limit: int = 100) -> Iterator[Story]:
        after, fetched = None, 0
        while fetched < limit:
            params = {"t": time_filter, "limit": min(100, limit - fetched), "raw_json": 1}
            if after:
                params["after"] = after
            data = _get(self.session, f"https://oauth.reddit.com/r/{subreddit}/top", params, self.sleep).json()
            children = data.get("data", {}).get("children", [])
            for child in children:
                fetched += 1
                if story := post_to_story(child.get("data", {})):
                    yield story
            after = data.get("data", {}).get("after")
            if not children or not after:
                return


class ArcticShiftSource:
    """Free Reddit archive API (no key). Scores are final ~36h after posting."""

    name = "arctic"
    META_FIELDS = "id,score,created_utc,is_self,over_18,title"
    FULL_FIELDS = "id,subreddit,title,author,score,num_comments,permalink,over_18,created_utc,is_self,stickied,selftext"

    def __init__(self, session: requests.Session | None = None, sleep: float = 0.2, max_pages: int = 2000):
        self.session = session or _session()
        self.sleep = sleep
        self.max_pages = max_pages

    @staticmethod
    def _items(payload) -> list[dict]:
        if isinstance(payload, dict):
            if payload.get("error"):
                raise RuntimeError(f"Arctic Shift error: {payload['error']}")
            payload = payload.get("data", [])
        return payload or []

    def _scan(self, subreddit: str, after: int | None) -> Iterator[dict]:
        """Walk every post in the subreddit oldest -> newest, metadata only (cheap)."""
        cursor, seen = after, 0
        for page in range(self.max_pages):
            params = {"subreddit": subreddit, "limit": "auto", "sort": "asc", "fields": self.META_FIELDS}
            if cursor:
                params["after"] = cursor
            items = self._items(_get(self.session, f"{ARCTIC_BASE}/posts/search", params, self.sleep).json())
            if not items:
                return
            yield from items
            seen += len(items)
            if page and page % 25 == 0:
                print(f"  r/{subreddit}: scanned {seen} posts...")
            newest = int(max(i.get("created_utc", 0) for i in items))
            if cursor is not None and newest <= cursor:
                return
            cursor = newest

    def top_posts(self, subreddit: str, time_filter: str = "all", limit: int = 100) -> Iterator[Story]:
        after = int(time.time() - TIME_WINDOWS[time_filter]) if time_filter in TIME_WINDOWS else None
        best: dict[str, dict] = {}
        for item in self._scan(subreddit, after):
            if item.get("is_self") is False:
                continue
            best[item["id"]] = item
        top_ids = [i["id"] for i in sorted(best.values(), key=lambda i: i.get("score", 0), reverse=True)[:limit]]
        for chunk in range(0, len(top_ids), 100):
            ids = ",".join(top_ids[chunk:chunk + 100])
            payload = _get(self.session, f"{ARCTIC_BASE}/posts/ids",
                           {"ids": ids, "fields": self.FULL_FIELDS}, self.sleep).json()
            posts = {p["id"]: p for p in self._items(payload)}
            for pid in top_ids[chunk:chunk + 100]:
                if pid in posts and (story := post_to_story(posts[pid])):
                    yield story


class RSSSource:
    """Reddit's public Atom feeds. Full text, no scores; feed order (= top ranking) is kept."""

    name = "rss"
    ATOM = "{http://www.w3.org/2005/Atom}"

    def __init__(self, session: requests.Session | None = None, sleep: float = 61.0):
        self.session = session or _session()
        self.sleep = sleep  # Reddit allows roughly one RSS request per minute without login

    def top_posts(self, subreddit: str, time_filter: str = "all", limit: int = 100) -> Iterator[Story]:
        url = f"https://www.reddit.com/r/{subreddit}/top/.rss"
        resp = _get(self.session, url, {"t": time_filter, "limit": min(limit, 100)}, self.sleep)
        yield from parse_rss(resp.text, subreddit)


def parse_rss(xml_text: str, subreddit: str) -> list[Story]:
    import xml.etree.ElementTree as ET

    ns = RSSSource.ATOM
    root = ET.fromstring(xml_text)
    entries = root.findall(f"{ns}entry")
    stories = []
    for rank, entry in enumerate(entries):
        content = entry.findtext(f"{ns}content") or ""
        md = re.search(r'<div class="md">(.*?)</div>\s*<!-- SC_ON -->', content, re.S)
        if not md:
            continue  # link / image post
        link = entry.find(f"{ns}link")
        post_id = (entry.findtext(f"{ns}id") or "").removeprefix("t3_")
        stories.append(Story(
            id=post_id,
            subreddit=subreddit,
            title=html.unescape(entry.findtext(f"{ns}title") or "").strip(),
            author=(entry.findtext(f"{ns}author/{ns}name") or "[deleted]").removeprefix("/u/"),
            # RSS has no score; keep the feed's top ranking as a stand-in so sorting still works.
            score=len(entries) - rank,
            num_comments=0,
            url=link.get("href", "") if link is not None else "",
            over_18=False,
            created_utc=0.0,
            text=clean_selftext(html_to_markdown(md.group(1))),
        ))
    return stories


def html_to_markdown(fragment: str) -> str:
    """Just enough HTML -> text for Reddit post bodies: keep paragraph breaks, drop tags."""
    text = re.sub(r"<br\s*/?>", "\n", fragment, flags=re.I)
    text = re.sub(r"</(p|li|h\d|blockquote|pre)>", "\n\n", text, flags=re.I)
    text = re.sub(r"<hr\s*/?>", "\n\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


SOURCES = {"reddit-api": RedditAPISource, "arctic": ArcticShiftSource, "rss": RSSSource}


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


def _auto_order() -> list[str]:
    creds = os.environ.get("REDDIT_CLIENT_ID") and os.environ.get("REDDIT_CLIENT_SECRET")
    return (["reddit-api"] if creds else []) + ["arctic", "rss"]


def scrape(subreddits: Iterable[str], time_filter: str, limit: int, source: str = "auto") -> list[Story]:
    """Fetch top stories per subreddit, falling back to the next source if one fails."""
    order = _auto_order() if source == "auto" else [source]
    stories: dict[str, Story] = {}
    for sub in subreddits:
        for name in order:
            try:
                src = SOURCES[name]()
                found = list(src.top_posts(sub, time_filter, limit))
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                print(f"[warn] r/{sub} via {name}: {exc}")
                continue
            print(f"r/{sub}: {len(found)} text posts via {name}")
            stories.update((s.id, s) for s in found)
            break
    return list(stories.values())


def save_stories(stories: list[Story], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([s.to_dict() for s in stories], indent=2, ensure_ascii=False))


def load_stories(path: Path) -> list[Story]:
    """Load our own dump, a raw Reddit listing JSON, or a JSONL file of raw posts
    (e.g. from the Arctic Shift download tool: https://arctic-shift.photon-reddit.com/download-tool)."""
    text = Path(path).read_text()
    if Path(path).suffix == ".jsonl":
        posts = [json.loads(line) for line in text.splitlines() if line.strip()]
        return [s for p in posts if (s := post_to_story(p))]
    data = json.loads(text)
    if isinstance(data, dict) and "data" in data:
        return [s for c in data["data"]["children"] if (s := post_to_story(c["data"]))]
    return [Story.from_dict(d) for d in data]
