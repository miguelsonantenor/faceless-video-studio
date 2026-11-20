"""Script markup (SCR-2, SCR-3, SCR-5)."""

from __future__ import annotations

import pytest

from fvs.errors import ErrorCode, FvsError
from fvs.script.markup import check_language, parse_script


def test_one_sentence_per_line() -> None:
    s = parse_script("First line here\n\n  Second line.  \nThird!")
    assert [x.spoken for x in s] == ["First line here", "Second line.", "Third!"]


def test_a_single_line_splits_at_sentence_ends() -> None:
    s = parse_script('It erupts. Then what? "Lightning!" she said. The end')
    assert [x.spoken for x in s] == ["It erupts.", "Then what?", '"Lightning!"',
                                     "she said.", "The end"]


def test_pairs_show_one_form_and_speak_the_other() -> None:
    (s,) = parse_script("It happens {1,000|one thousand} times a year.")
    assert s.caption == "It happens 1,000 times a year."
    assert s.spoken == "It happens one thousand times a year."
    assert s.word_count == 7


def test_emphasis_marks_caption_words_and_keeps_punctuation() -> None:
    s, t = parse_script("Nobody knows *exactly why*, really.\n*{NASA|nasa}*'s fault.")
    assert s.caption == "Nobody knows exactly why, really."
    assert s.emphasis == [2, 3]
    assert (t.caption, t.spoken, t.emphasis) == ("NASA's fault.", "nasa's fault.", [0])


def test_pauses_attach_to_the_sentence_before_and_split_a_line() -> None:
    s = parse_script("Wait for it [pause 0.8] here it comes. [pause 1.2]\nBoom.")
    assert [(x.spoken, x.pause_after_s) for x in s] == [
        ("Wait for it", 0.8), ("here it comes.", 1.2), ("Boom.", 0.0)]


def test_caption_only_and_spoken_only_pairs() -> None:
    (s,) = parse_script("Look {→|} {|um} there.")
    assert s.caption == "Look → there."
    assert s.spoken == "Look um there."


@pytest.mark.parametrize(("text", "needle"), [
    ("", "empty"),
    ("An *open emphasis here.", "Unbalanced"),
    ("A {broken pair here.", "Unbalanced"),
    ("A | pipe.", "outside a pair"),
    ("[pause 0.5] Starts with a pause.", "starts with a pause"),
    ("Too long a pause. [pause 5]", "outside 0.1–3 s"),
    ("[voice: anna] Hello.", "phase 3"),
    ("Hello [music] there.", "Unknown markup"),
    ("{shown|}", "Nothing would be spoken"),
])
def test_markup_errors_say_what_to_fix(text: str, needle: str) -> None:
    with pytest.raises(FvsError) as info:
        parse_script(text)
    assert info.value.code == ErrorCode.SCRIPT_INVALID
    assert needle in info.value.message and info.value.fix and info.value.example


@pytest.mark.parametrize("text", ["火山は自分の雷を作ります。", "ภูเขาไฟสร้างฟ้าผ่าของตัวเอง"])
def test_languages_without_spaces_are_refused(text: str) -> None:
    with pytest.raises(FvsError) as info:
        check_language(text)
    assert info.value.code == ErrorCode.LANGUAGE_UNSUPPORTED


@pytest.mark.parametrize("text", ["El volcán crea sus propios rayos.", "화산은 번개를 만든다",
                                  "The word 東京 means Tokyo."])
def test_languages_with_spaces_are_accepted(text: str) -> None:
    check_language(text)
