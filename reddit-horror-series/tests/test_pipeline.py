import json
from pathlib import Path

import pytest

from horror_series.cli import main
from horror_series.reddit import clean_selftext, load_stories, post_to_story
from horror_series.series import build_series, choose_cuts, is_candidate, rank_stories
from horror_series.text import cliffhanger_score, hook_score, split_sentences

FIXTURES = Path(__file__).parent / "fixtures"
STORY = (FIXTURES / "the_night_shift.txt").read_text()


def _post(id_, title, text, **kw):
    return {"id": id_, "title": title, "selftext": text, "is_self": True, "subreddit": "nosleep",
            "author": "tester", "score": kw.pop("score", 5000), "num_comments": 10,
            "permalink": f"/r/nosleep/comments/{id_}/", "over_18": False, "created_utc": 0, **kw}


@pytest.fixture
def listing(tmp_path):
    data = {"kind": "Listing", "data": {"after": None, "children": [
        {"kind": "t3", "data": _post("aaa", "I worked the night shift at a storage facility", STORY)},
        {"kind": "t3", "data": _post("bbb", "Too short to serialise", "It was dark. Then it was not.")},
        {"kind": "t3", "data": _post("ccc", "The storage facility [Part 2]", STORY)},
        {"kind": "t3", "data": _post("ddd", "A link post", "", is_self=False)},
        {"kind": "t3", "data": _post("eee", "Removed", "[removed]")},
    ]}}
    path = tmp_path / "listing.json"
    path.write_text(json.dumps(data))
    return path


def test_clean_selftext_strips_markdown_and_author_notes():
    raw = "**Bold** and [a link](http://x.y) &amp; *italic*\n\n# Heading\n\nReal text here, honestly.\n\nEdit: thanks for the gold!"
    out = clean_selftext(raw)
    assert out == "Bold and a link & italic\n\nHeading\n\nReal text here, honestly."


def test_load_raw_listing_skips_non_text_posts(listing):
    ids = {s.id for s in load_stories(listing)}
    assert ids == {"aaa", "bbb", "ccc"}


def test_candidate_filter(listing):
    stories = {s.id: s for s in load_stories(listing)}
    assert is_candidate(stories["aaa"])[0]
    assert "short" in is_candidate(stories["bbb"])[1]
    assert "series" in is_candidate(stories["ccc"])[1]


def test_cuts_cover_story_and_are_balanced():
    sents = split_sentences(STORY)
    cuts = choose_cuts(sents, 10)
    assert len(cuts) == 10 and cuts[-1] == len(sents) - 1
    assert cuts == sorted(set(cuts))
    starts = [0] + [c + 1 for c in cuts[:-1]]
    sizes = [sum(s.words for s in sents[a:b + 1]) for a, b in zip(starts, cuts)]
    target = sum(sizes) / 10
    assert all(0.55 * target <= n <= 1.6 * target for n in sizes)


def test_cliffhangers_prefer_tension_over_filler():
    sents = split_sentences("I made coffee and sat down at the desk to start my shift, like always.\n\n"
                            "Then I heard it. Someone was knocking on the glass behind me.")
    assert cliffhanger_score(sents, 2) > cliffhanger_score(sents, 0)


def test_hook_prefers_standalone_punchy_lines():
    good, bad = split_sentences("The ringing was coming from inside unit 214. "
                                "Then we went to the store and bought some groceries for the week ahead of time.")
    assert hook_score(good) > hook_score(bad)


def test_series_structure():
    story = post_to_story(_post("aaa", "Night shift", STORY))
    series = build_series(story)
    assert len(series.parts) == 10
    # Nothing lost or duplicated: story + cliffhanger of all parts == original sentences.
    rebuilt = " ".join(" ".join((p.story, p.cliffhanger)) for p in series.parts)
    rebuilt = " ".join(rebuilt.split())
    assert rebuilt == " ".join(STORY.split())
    for p in series.parts:
        assert p.hook and p.cliffhanger and p.call_to_action
        assert f"Part {p.number}/10" in p.title
        script = p.script()
        assert script.index(p.hook) < script.index(p.story) < script.index(p.cliffhanger)
    assert series.parts[0].recap == ""
    for prev, part in zip(series.parts, series.parts[1:]):
        # Every episode either re-states the previous cliffhanger in its hook or in its recap.
        assert prev.cliffhanger in part.hook or prev.cliffhanger in part.recap
        assert part.recap.startswith(f"Part {part.number}.")
    assert "Part 2" in series.parts[0].call_to_action
    assert "final part" in series.parts[-1].call_to_action
    # Part 1's cold open must not spoil the finale.
    assert series.parts[-1].story.split(". ")[0] not in series.parts[0].hook


def test_cli_build_writes_outputs(listing, tmp_path, capsys):
    out = tmp_path / "out"
    assert main(["build", "--stories", str(listing), "--out", str(out), "--top", "3"]) == 0
    folders = [p for p in out.iterdir() if p.is_dir()]
    assert len(folders) == 1
    files = sorted(f.name for f in folders[0].iterdir())
    assert files == [f"part_{i:02d}.txt" for i in range(1, 11)] + ["series.json", "series.md"]
    assert (out / "leaderboard.md").exists()


def test_rank_stories_orders_by_fitness(listing):
    stories = load_stories(listing)
    ranked = rank_stories(stories)
    assert [s.story_id for s in ranked] == ["aaa"]
    assert ranked[0].fitness["parts_in_length_range"] > 0


def test_llm_packaging_keeps_story_text():
    from types import SimpleNamespace

    from horror_series.llm import PartPackaging, SeriesPackaging, package_with_claude

    story = post_to_story(_post("aaa", "Night shift", STORY))
    series = build_series(story)
    original = [(p.story, p.hook) for p in series.parts]
    parsed = SeriesPackaging(series_title="The Night Shift", parts=[
        PartPackaging(number=i, hook=f"hook {i}", hook_alternates=["alt"], recap=f"Part {i}. recap",
                      cliffhanger_voiceover="And it was still there.", call_to_action="Next!",
                      title=f"Night Shift Part {i}", thumbnail_text="DON'T ANSWER")
        for i in range(1, 11)])
    calls = []

    class FakeMessages:
        def parse(self, **kw):
            calls.append(kw)
            return SimpleNamespace(stop_reason="end_turn", parsed_output=parsed)

    client = SimpleNamespace(beta=SimpleNamespace(messages=FakeMessages()))
    package_with_claude(story, series, client=client)
    assert calls[0]["model"] == "claude-opus-5"
    assert series.series_title == "The Night Shift"
    for p, (story_text, old_hook) in zip(series.parts, original):
        assert p.story == story_text
        assert p.hook == f"hook {p.number}" and old_hook in p.hook_alternates
    assert series.parts[0].recap == ""
    assert series.parts[-1].cliffhanger.endswith("knuckle.")  # finale gets no voice-over tease
    assert "still there" in series.parts[0].cliffhanger
