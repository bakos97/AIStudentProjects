"""Offline tests for the Reddit sources, using a fake HTTP session."""

import json

import requests

from horror_series import reddit
from horror_series.reddit import ArcticShiftSource, RSSSource, parse_rss, scrape

LONG = "The door opened by itself. " * 5


class FakeResponse:
    def __init__(self, payload=None, text="", status=200):
        self.payload, self.text, self.status_code, self.headers = payload, text, status, {}

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


class FakeSession:
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        return self.handler(url, params or {})


def _meta(i, score, t, is_self=True):
    return {"id": f"p{i}", "score": score, "created_utc": t, "is_self": is_self, "over_18": False, "title": f"T{i}"}


def test_arctic_scans_ranks_by_score_and_fetches_full_text():
    pages = [
        {"data": [_meta(1, 10, 100), _meta(2, 900, 200), _meta(3, 5000, 300, is_self=False)]},
        {"data": [_meta(4, 50, 400), _meta(5, 700, 500)]},
        {"data": []},
    ]

    def handler(url, params):
        if url.endswith("/posts/search"):
            return FakeResponse(pages.pop(0))
        ids = params["ids"].split(",")
        return FakeResponse({"data": [
            {"id": i, "subreddit": "nosleep", "title": i, "author": "a", "score": 1, "num_comments": 0,
             "permalink": f"/r/nosleep/comments/{i}/", "over_18": False, "created_utc": 0,
             "is_self": True, "selftext": LONG} for i in reversed(ids)]})

    session = FakeSession(handler)
    stories = list(ArcticShiftSource(session=session, sleep=0).top_posts("nosleep", "all", limit=2))
    assert [s.id for s in stories] == ["p2", "p5"]  # the link post p3 is skipped
    search_calls = [p for u, p in session.calls if u.endswith("/posts/search")]
    assert search_calls[1]["after"] == 300 and search_calls[2]["after"] == 500  # paginates by time
    assert "after" not in search_calls[0]  # --time all scans the whole history


def test_arctic_time_filter_sets_start():
    session = FakeSession(lambda url, params: FakeResponse({"data": []}))
    list(ArcticShiftSource(session=session, sleep=0).top_posts("nosleep", "year", limit=5))
    assert "after" in session.calls[0][1]


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <author><name>/u/night_guard</name></author>
    <content type="html">&lt;!-- SC_OFF --&gt;&lt;div class="md"&gt;&lt;p&gt;I worked nights. &lt;em&gt;Never&lt;/em&gt; again.&lt;/p&gt;
&lt;p&gt;The phone rang at 3 AM &amp;amp; nobody was there.&lt;/p&gt;
&lt;/div&gt;&lt;!-- SC_ON --&gt; &amp;#32; submitted by &lt;a href="https://www.reddit.com/user/night_guard"&gt; /u/night_guard &lt;/a&gt;</content>
    <id>t3_abc123</id>
    <link href="https://www.reddit.com/r/nosleep/comments/abc123/the_night_shift/" />
    <title>The night shift</title>
  </entry>
  <entry>
    <author><name>/u/someone</name></author>
    <content type="html">&lt;a href="https://i.redd.it/x.jpg"&gt;[link]&lt;/a&gt;</content>
    <id>t3_img999</id>
    <link href="https://www.reddit.com/r/nosleep/comments/img999/" />
    <title>An image post</title>
  </entry>
</feed>"""


def test_parse_rss_extracts_full_text_and_skips_link_posts():
    stories = parse_rss(RSS, "nosleep")
    assert len(stories) == 1
    s = stories[0]
    assert (s.id, s.author, s.title) == ("abc123", "night_guard", "The night shift")
    assert s.text == "I worked nights. Never again.\n\nThe phone rang at 3 AM & nobody was there."
    assert s.url.endswith("/abc123/the_night_shift/")


def test_rss_source_requests_top_feed():
    session = FakeSession(lambda url, params: FakeResponse(text=RSS))
    stories = list(RSSSource(session=session, sleep=0).top_posts("nosleep", "all", limit=100))
    assert session.calls[0] == ("https://www.reddit.com/r/nosleep/top/.rss", {"t": "all", "limit": 100})
    assert len(stories) == 1


def test_scrape_falls_back_to_next_source(monkeypatch, capsys):
    class Broken:
        def top_posts(self, *a):
            raise requests.HTTPError("403 Forbidden")

    class Works:
        def top_posts(self, sub, *a):
            return parse_rss(RSS, sub)

    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    monkeypatch.setitem(reddit.SOURCES, "arctic", Broken)
    monkeypatch.setitem(reddit.SOURCES, "rss", Works)
    stories = scrape(["nosleep"], "all", 10)
    assert [s.id for s in stories] == ["abc123"]
    out = capsys.readouterr().out
    assert "via arctic: 403" in out and "via rss" in out


def test_reddit_api_source_requires_credentials(monkeypatch):
    monkeypatch.delenv("REDDIT_CLIENT_ID", raising=False)
    assert reddit._auto_order() == ["arctic", "rss"]
    monkeypatch.setenv("REDDIT_CLIENT_ID", "x")
    monkeypatch.setenv("REDDIT_CLIENT_SECRET", "y")
    assert reddit._auto_order()[0] == "reddit-api"


def test_arctic_error_payload_raises():
    session = FakeSession(lambda url, params: FakeResponse({"error": "Timeout", "data": None}))
    try:
        list(ArcticShiftSource(session=session, sleep=0).top_posts("nosleep", "all", 5))
    except RuntimeError as exc:
        assert "Timeout" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def test_load_jsonl_dump(tmp_path):
    from horror_series.reddit import load_stories

    path = tmp_path / "r_nosleep_posts.jsonl"
    post = {"id": "q1", "subreddit": "nosleep", "title": "t", "author": "a", "score": 3, "num_comments": 0,
            "permalink": "/r/nosleep/comments/q1/", "over_18": False, "created_utc": 1, "is_self": True,
            "selftext": LONG}
    path.write_text(json.dumps(post) + "\n" + json.dumps({**post, "id": "q2", "is_self": False}) + "\n")
    assert [s.id for s in load_stories(path)] == ["q1"]
