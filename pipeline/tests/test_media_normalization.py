"""Assembly failures must be recoverable and understandable without provider work."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from serial import media


def _clip(path: Path, *, audio: bool = True) -> Path:
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
        "color=c=blue:s=160x284:r=30:d=1",
    ]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=1"]
    cmd += ["-c:v", "libx264"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True)
    return path


@pytest.mark.parametrize("audio", [True, False])
def test_normalization_selects_one_video_and_audio_track(audio, tmp_path):
    source = _clip(tmp_path / "source.mp4", audio=audio)
    result = media.normalize_clip(source, tmp_path / "normal.mp4", 320, 568,
                                  scene_id="sc07")
    info = media.probe(result)
    assert (info["width"], info["height"]) == (320, 568)
    assert info["fps"] == "24/1"
    assert info["has_audio"] is True


def test_failed_normalization_keeps_previous_complete_file(tmp_path, monkeypatch):
    source = _clip(tmp_path / "source.mp4")
    dest = tmp_path / "normal.mp4"
    dest.write_bytes(b"previous complete clip")
    real_run = media.subprocess.run

    def fail(cmd, check, capture_output, text):
        if cmd[0] != "ffmpeg":
            return real_run(cmd, check=check, capture_output=capture_output, text=text)
        Path(cmd[-1]).write_bytes(b"partial encode")
        raise subprocess.CalledProcessError(1, cmd, stderr="No space left on device")

    monkeypatch.setattr(media.subprocess, "run", fail)
    with pytest.raises(media.MediaCommandError, match=r"scene sc12.*No space left"):
        media.normalize_clip(source, dest, 320, 568, scene_id="sc12")
    assert dest.read_bytes() == b"previous complete clip"
    assert not list(tmp_path.glob(".normalize-*"))


def test_media_error_is_bounded_and_hides_urls():
    original = subprocess.CalledProcessError(
        1, ["ffmpeg", "secret-local-path"],
        stderr="https://storage.example/private?token=secret " + "x" * 1200,
    )
    message = str(media.MediaCommandError(original, "scene sc03"))
    assert "scene sc03" in message
    assert "token=secret" not in message
    assert "secret-local-path" not in message
    assert len(message) < 1000


def test_killed_encoder_names_worker_memory():
    original = subprocess.CalledProcessError(-9, ["ffmpeg"], stderr="")
    assert "memory" in str(media.MediaCommandError(original, "scene sc01"))
