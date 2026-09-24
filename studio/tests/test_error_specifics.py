"""Guidance must keep the ids the engine already worked out.

A live voice stage stopped with "A spoken line is too long for its clip.
Shorten it in the script and save again." — and named no scene. The engine
had computed exactly which scenes overran and by how much; the translation
layer replaced the whole message with advice and discarded it, leaving eight
scenes to guess between.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

STUDIO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(STUDIO))
sys.path.insert(0, str(STUDIO.parent / "pipeline"))

from app import i18n  # noqa: E402
from app.runner import _GUIDANCE, explain, specifics  # noqa: E402

OVERRUN = ("RuntimeError: реплики не помещаются даже с atempo 1.15: "
           "['sc05 3.84s > 3.75s (+0.09s)', 'sc08 5.90s > 3.75s (+2.15s)']. "
           "Сократи текст в brief.json")


def _ru(text):
    request = SimpleNamespace(cookies={i18n.COOKIE: "ru"})
    return i18n.notice({"request": request}, text)


def test_the_overrunning_scenes_survive_the_guidance():
    message = explain(OVERRUN)
    assert "too long for its clip" in message
    assert "sc05 3.84s > 3.75s (+0.09s)" in message
    assert "sc08 5.90s > 3.75s (+2.15s)" in message


def test_the_missing_voice_character_survives():
    message = explain("RuntimeError: нет voice id в .env для: nora (ELEVENLABS_VOICE_ID_NORA)")
    assert "nora" in message and "ELEVENLABS_VOICE_ID_NORA" in message


def test_the_failed_qc_scenes_survive():
    assert "sc03" in explain("RuntimeError: keyframes не прошли QC: ['sc03']. Поправь prompts")


def test_guidance_without_specifics_stays_clean():
    """No empty brackets when the engine named nothing."""
    message = explain("RuntimeError: PIPELINE_ALLOW_PAID не установлен")
    assert message and "(" not in message


def test_an_unknown_error_still_gets_no_guidance():
    assert explain("RuntimeError: something entirely unrecognised") == ""


def test_specifics_are_bounded_and_tidy():
    """A runaway engine message must not flood the error banner."""
    assert len(specifics("RuntimeError: не помещаются: ['" + "x" * 900 + "']")) <= 300
    assert specifics("RuntimeError: no identifiers here") == ""


# ── Russian ────────────────────────────────────────────────────────────────

def test_every_guidance_string_has_a_russian_translation():
    """Five of these had none, so a Russian operator read English advice."""
    ru = json.loads((STUDIO / "app" / "locales" / "ru.json").read_text())
    assert [advice for _, advice in _GUIDANCE if advice not in ru] == []


def test_russian_translates_the_sentence_and_keeps_the_ids():
    translated = _ru("RuntimeError: " + explain(OVERRUN))
    assert "Реплика не помещается в свой клип" in translated
    # Scene ids and measurements are data, not wording.
    assert "sc05 3.84s > 3.75s (+0.09s)" in translated
    assert "too long" not in translated


def test_russian_still_translates_guidance_without_ids():
    translated = _ru("RuntimeError: " + explain("RuntimeError: нужен approval 'publish'"))
    assert "Утвердите готовый эпизод" in translated


# ── the overrun figures themselves ─────────────────────────────────────────

def test_a_small_overrun_is_not_printed_as_an_equality():
    """Production showed "sc05 (3.8s > 3.8s)", which reads as a broken
    comparison. One decimal rounded both sides to the same value."""
    from serial.pipeline import _overrun
    line = _overrun("sc05", 3.84, 3.75)
    assert "3.8s > 3.8s" not in line
    assert "3.84s" in line and "3.75s" in line


def test_the_overrun_states_how_much_to_cut():
    """The actionable number is the difference, not the two endpoints."""
    from serial.pipeline import _overrun
    assert "(+0.09s)" in _overrun("sc05", 3.84, 3.75)
    assert "(+2.15s)" in _overrun("sc08", 5.90, 3.75)


def test_overrun_figures_are_language_neutral():
    """They travel inside a Russian engine message into an English or Russian
    panel, so they must carry no words."""
    from serial.pipeline import _overrun
    line = _overrun("sc05", 3.84, 3.75)
    assert all(ch.isascii() for ch in line)
    assert not any(ch.isalpha() for ch in line.replace("sc", "").replace("s", ""))


# ── the word budget the validator enforces ─────────────────────────────────

def test_the_budget_matches_the_room_the_voice_stage_leaves():
    """The validator measured words against the full clip while the voice
    stage spends part of it on lead-in, gaps and tail. A brief could pass and
    then prove impossible to voice, after its video had been paid for."""
    from serial.package import GAP, LEAD_IN, TAIL, speech_room, word_budget
    assert speech_room(4, 1) == 4 - TAIL - LEAD_IN
    assert speech_room(4, 2) == 4 - TAIL - LEAD_IN - GAP
    # More lines, less room, so a smaller budget.
    assert word_budget(4, 3) < word_budget(4, 2) < word_budget(4, 1)


def test_the_budget_never_reaches_zero():
    from serial.package import word_budget
    assert word_budget(4, 20) >= 1


def test_validation_and_production_share_one_set_of_constants():
    """They drifted because each module had its own copy."""
    from serial import package, pipeline
    assert pipeline.LEAD_IN is package.LEAD_IN
    assert pipeline.GAP is package.GAP
    assert pipeline.MAX_TEMPO is package.MAX_TEMPO


def test_an_unrecognised_failure_still_names_itself():
    """A failure with no advice was replaced by a sentence about uncertain
    requests, so the screen described a situation that may not be this one and
    said nothing about what actually broke."""
    import inspect
    from app import live_jobs
    source = inspect.getsource(live_jobs.run)
    assert "f'{type(exc).__name__}: {advice}' if advice else (" in source
    assert "f'{type(exc).__name__}. Production stopped." in source


def test_ffmpeg_failure_keeps_its_safe_scene_diagnostic():
    """The original Episode 4 failure showed only CalledProcessError."""
    import inspect
    from app import live_jobs
    source = inspect.getsource(live_jobs.run)
    assert "MediaCommandError" in source
