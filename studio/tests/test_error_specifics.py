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
           "['sc05 (4.8s > 3.6s)', 'sc07 (4.2s > 3.6s)']. Сократи текст в brief.json")


def _ru(text):
    request = SimpleNamespace(cookies={i18n.COOKIE: "ru"})
    return i18n.notice({"request": request}, text)


def test_the_overrunning_scenes_survive_the_guidance():
    message = explain(OVERRUN)
    assert "too long for its clip" in message
    assert "sc05 (4.8s > 3.6s)" in message and "sc07 (4.2s > 3.6s)" in message


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
    assert "sc05 (4.8s > 3.6s)" in translated
    assert "too long" not in translated


def test_russian_still_translates_guidance_without_ids():
    translated = _ru("RuntimeError: " + explain("RuntimeError: нужен approval 'publish'"))
    assert "Утвердите готовый эпизод" in translated
