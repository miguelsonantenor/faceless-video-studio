"""Character timings → caption word timings (DS 5.4 step 5)."""

from __future__ import annotations

from fvs.script.markup import parse_script
from fvs.voice.timing import caption_words, word_times


def even_alignment(text: str, seconds_per_char: float = 0.1) -> dict:
    return {"characters": list(text),
            "character_start_times_seconds": [i * seconds_per_char for i in range(len(text))],
            "character_end_times_seconds": [(i + 1) * seconds_per_char
                                            for i in range(len(text))]}


def test_word_times_from_characters() -> None:
    words = word_times(even_alignment("Hi  there."))
    assert [(w, round(s, 2), round(e, 2)) for w, s, e in words] == [
        ("Hi", 0.0, 0.2), ("there.", 0.4, 1.0)]


def test_pairs_take_the_time_of_their_spoken_words() -> None:
    (s,) = parse_script("It happens {1,000|one thousand} *times*.")
    words = caption_words(s.groups, even_alignment(s.spoken), 4.0)
    assert [w["word"] for w in words] == ["It", "happens", "1,000", "times."]
    one_thousand = (s.spoken.index("one") * 0.1, (s.spoken.index("thousand") + 8) * 0.1)
    assert (words[2]["start"], words[2]["end"]) == tuple(round(t, 3) for t in one_thousand)
    assert [w["emph"] for w in words] == [False, False, False, True]


def test_a_multiword_caption_shares_its_span() -> None:
    (s,) = parse_script("Visit {New York City|NYC} today.")
    words = caption_words(s.groups, even_alignment(s.spoken), 2.0)
    new, york, city = words[1:4]
    assert new["start"] == 0.6 and city["end"] == 0.9
    assert new["end"] == york["start"] and york["end"] == city["start"]


def test_caption_only_words_take_no_time() -> None:
    (s,) = parse_script("Look {→|} there.")
    words = caption_words(s.groups, even_alignment(s.spoken), 2.0)
    assert words[1]["word"] == "→" and words[1]["start"] == words[1]["end"] == words[0]["end"]


def test_word_count_mismatch_spreads_by_length() -> None:
    (s,) = parse_script("It costs {$5|five dollars} now.")
    alignment = even_alignment("It costs five-dollars now.")  # normalised into fewer words
    words = caption_words(s.groups, alignment, 3.0)
    assert [w["word"] for w in words] == ["It", "costs", "$5", "now."]
    assert words[0]["start"] == 0.0 and words[-1]["end"] == 2.6
    assert all(a["end"] <= b["start"] + 1e-9 for a, b in zip(words, words[1:], strict=False))
