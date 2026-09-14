"""Stage direction must not be read aloud.

sc08's line is "You knew?" — two words — and it voiced as 5.86 seconds. Its
delivery note is sixteen words of direction, and the voice stage wrapped the
whole thing in square brackets and sent it to the model with the line.

eleven_v3 audio tags are short auditory cues: [whispers], [sighs],
[sarcastic]. A sentence of direction is not a tag, and the model reads it
out. This is why lines overran their clips, and the likeliest cause of the
standing complaint that the speech is unclear.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from serial.providers import MAX_TAG_WORDS, audio_tag, tts  # noqa: E402

PRODUCTION_NOTES = [
    "low, controlled, clearly articulated American English; a question addressed to Adrian, "
    "hurt turning into suspicion",
    "quiet, guilty, clear conversational American English; a short pause after Nora; "
    "restrained, not theatrical",
]


@pytest.mark.parametrize("note", PRODUCTION_NOTES)
def test_the_direction_that_was_being_voiced_is_not_a_tag(note):
    assert audio_tag(note) == ""


@pytest.mark.parametrize("cue", ["whispers", "sighs", "sarcastic", "quiet, guilty", "low, controlled"])
def test_real_cues_still_become_tags(cue):
    assert audio_tag(cue) == cue


def test_a_sentence_is_never_a_tag():
    """Punctuation that ends a clause marks prose, whatever its length."""
    assert audio_tag("quiet. guilty") == ""
    assert audio_tag("quiet; guilty") == ""
    assert audio_tag("as follows: quiet") == ""


def test_the_word_limit_is_the_boundary():
    assert audio_tag(" ".join(["calm"] * MAX_TAG_WORDS)) != ""
    assert audio_tag(" ".join(["calm"] * (MAX_TAG_WORDS + 1))) == ""


def test_whitespace_is_normalised():
    assert audio_tag("  quiet,\n  guilty  ") == "quiet, guilty"


def test_an_empty_delivery_produces_no_tag():
    assert audio_tag("") == "" and audio_tag(None) == ""


# ── what actually reaches the model ────────────────────────────────────────

def _spoken(delivery, text="You knew?", model="eleven_v3"):
    logged = []
    prov = tts(SimpleNamespace(elevenlabs_model_id=model, dry_run=True, elevenlabs_api_key=""),
               logged.append, text, delivery, "voice-id", Path("/tmp/markevita-tag-test.mp3"))
    return prov["spoken_text"], logged


def test_direction_is_stripped_from_what_is_spoken():
    spoken, _ = _spoken(PRODUCTION_NOTES[0])
    assert spoken == "You knew?"
    assert "Adrian" not in spoken and "suspicion" not in spoken


def test_a_real_cue_is_still_sent():
    spoken, _ = _spoken("quiet, guilty")
    assert spoken == "[quiet, guilty] You knew?"


def test_dropping_the_direction_is_reported():
    """Silently ignoring it would hide why a delivery note had no effect."""
    _, logged = _spoken(PRODUCTION_NOTES[0])
    assert any("not an audio tag" in line for line in logged)


def test_nothing_is_reported_when_the_cue_is_used():
    _, logged = _spoken("whispers")
    assert not any("not an audio tag" in line for line in logged)


def test_older_models_never_receive_tags():
    """Audio tags are an eleven_v3 feature."""
    spoken, _ = _spoken("quiet, guilty", model="eleven_multilingual_v2")
    assert spoken == "You knew?"


def test_the_line_itself_is_recorded_unchanged():
    prov = tts(SimpleNamespace(elevenlabs_model_id="eleven_v3", dry_run=True, elevenlabs_api_key=""),
               lambda m: None, "You knew?", PRODUCTION_NOTES[0], "voice-id",
               Path("/tmp/markevita-tag-test.mp3"))
    assert prov["text"] == "You knew?"


def test_cached_audio_from_the_old_behaviour_is_not_reused():
    """The cache key is the request parameters, which did not change with this
    fix. Without a marker, every line already voiced would come back with the
    direction still in it."""
    source = (STUDIO.parent / "pipeline" / "serial" / "pipeline.py").read_text()
    assert '"tag_policy": 2' in source
