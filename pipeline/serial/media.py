"""ffmpeg-обвязка для сборки и автоматического QA."""

import json
import math
import re
import subprocess
import tempfile
from pathlib import Path


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return p.stderr


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height,r_frame_rate",
                          "-of", "json", str(path)], check=True, capture_output=True, text=True).stdout
    j = json.loads(out)
    v = next((s for s in j.get("streams", []) if s.get("codec_type") == "video"), {})
    return {"duration": float(j["format"]["duration"]), "width": v.get("width"), "height": v.get("height"),
            "fps": v.get("r_frame_rate"), "has_audio": any(s.get("codec_type") == "audio" for s in j.get("streams", []))}


def duration(path: Path) -> float:
    return probe(path)["duration"]


def sample_frames(video: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    d = duration(video)
    paths = []
    for i, t in enumerate((0.1, 0.5, 0.9)):
        p = out_dir / f"frame_{i}.jpg"
        _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{max(0.0, d*t - 0.04):.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "3",
              "-vf", "scale=720:-2", str(p)])
        paths.append(p)
    return paths


def poster_frame(video: Path, t: float, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(dest)])
    return dest


# ---------- audio ----------

def phone_fx(src: Path, dest: Path) -> Path:
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af",
          "highpass=f=300,lowpass=f=3400,acompressor=threshold=-18dB:ratio=3,volume=-2dB", "-c:a", "libmp3lame", str(dest)])
    return dest


def speed_up(src: Path, dest: Path, factor: float) -> Path:
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-af", f"atempo={factor:.3f}", "-c:a", "libmp3lame", str(dest)])
    return dest


def build_timeline(lines: list[tuple[Path, float]], total: float, dest: Path, sr: int = 44100) -> list[dict]:
    """Кладёт реплики последовательно на дорожку длиной total. lines = [(path, start_sec)]. Возвращает тайминги."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not lines:
        _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={sr}",
              "-t", f"{total:.3f}", "-c:a", "libmp3lame", str(dest)])
        return []
    inputs, chain, labels, timing = [], "", "", []
    for i, (p, start) in enumerate(lines):
        inputs += ["-i", str(p)]
        ms = int(start * 1000)
        chain += f"[{i}:a]aformat=sample_rates={sr}:channel_layouts=stereo,adelay={ms}|{ms}[l{i}];"
        labels += f"[l{i}]"
        timing.append({"index": i, "start": round(start, 3), "end": round(start + duration(p), 3)})
    chain += f"{labels}amix=inputs={len(lines)}:duration=longest:normalize=0,apad,atrim=0:{total:.3f}[out]"
    _run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", chain, "-map", "[out]", "-c:a", "libmp3lame", "-b:a", "192k", str(dest)])
    return timing


def mux_audio(video: Path, audio: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0",
          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(dest)])
    return dest


def mix_audio_into(video: Path, extra_audio: Path, dest: Path, extra_db: float = 0.0) -> Path:
    """Подмешать дорожку (напр. VO поверх lipsync-видео)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    p = probe(video)
    if not p["has_audio"]:
        return mux_audio(video, extra_audio, dest)
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(extra_audio), "-filter_complex",
          f"[1:a]volume={extra_db}dB[x];[0:a][x]amix=inputs=2:duration=first:normalize=0[a]", "-map", "0:v:0", "-map", "[a]",
          "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", str(dest)])
    return dest


# ---------- assembly ----------

def normalize_clip(src: Path, dest: Path, w: int, h: int, fps: int = 24) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    vf = f"scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps={fps},format=yuv420p"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src)]
    if not probe(src)["has_audio"]:
        cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000", "-shortest"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2", str(dest)]
    _run(cmd)
    return dest


def concat(clips: list[Path], dest: Path) -> Path:
    lst = dest.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in clips), encoding="utf-8")
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(dest)])
    return dest


def add_bed(video: Path, bed: Path, dest: Path, bed_db: float) -> Path:
    """Room tone / музыка под диалог, зациклено, с fade out."""
    d = duration(video)
    filt = f"[1:a]volume={bed_db}dB,afade=t=out:st={max(0, d-1.5):.2f}:d=1.5[m];[0:a][m]amix=inputs=2:duration=first:normalize=0[a]"
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-stream_loop", "-1", "-i", str(bed), "-filter_complex", filt,
          "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(dest)])
    return dest


def score_track(segments: list[dict], dest: Path) -> Path:
    """One music track for the whole episode, cut to the scenes under it.

    A single bed under everything was the only shape the engine had, so the
    music could not know that one scene is a confession and the next is a
    boat leaving. Each segment names its own bed and its own level; the bed
    is looped to the segment's length, faded at both ends so the change of
    mood does not arrive as a click, and the segments are laid end to end.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    parts, work = [], dest.parent / f"{dest.stem}_parts"
    work.mkdir(parents=True, exist_ok=True)
    for i, seg in enumerate(segments):
        length = float(seg["seconds"])
        if length <= 0:
            continue
        fade = min(0.8, length / 2)
        part = work / f"{i:03d}.wav"
        filt = (f"volume={float(seg['db'])}dB,"
                f"afade=t=in:st=0:d={fade:.2f},"
                f"afade=t=out:st={max(0.0, length - fade):.2f}:d={fade:.2f}")
        _run(["ffmpeg", "-y", "-loglevel", "error", "-stream_loop", "-1", "-i", str(seg["bed"]),
              "-t", f"{length:.3f}", "-af", filt, "-ar", "48000", "-ac", "2", str(part)])
        parts.append(part)
    if not parts:
        raise RuntimeError("A music track needs at least one scene to play under.")
    lst = work / "parts.txt"
    lst.write_text("".join(f"file '{p.resolve()}'\n" for p in parts), encoding="utf-8")
    _run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
          "-ar", "48000", "-ac", "2", str(dest)])
    return dest


def mix_track(video: Path, track: Path, dest: Path) -> Path:
    """Lay a finished track under the video without looping or re-levelling it.

    add_bed loops its input forever and sets one level for the whole episode,
    which is right for room tone and wrong for a score that already carries
    its own shape.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    filt = "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]"
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-i", str(track), "-filter_complex", filt,
          "-map", "0:v:0", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", str(dest)])
    return dest


def loudnorm(video: Path, dest: Path, i: float = -14.0, tp: float = -1.5, lra: float = 11.0) -> Path:
    """Measure the entire soundtrack, then normalize using those measurements.

    Video packets are copied unchanged. Write atomically so an interrupted
    audio encode cannot replace a complete master or leave a partial result.
    https://ffmpeg.org/ffmpeg-filters.html#loudnorm
    """
    target = f"loudnorm=I={i}:TP={tp}:LRA={lra}"
    report = _run(["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(video),
                   "-map", "0:a:0", "-af", target + ":print_format=json", "-f", "null", "-"])
    blocks = re.findall(r'\{[^{}]*"input_i"[^{}]*\}', report)
    if not blocks:
        raise RuntimeError("Audio normalization could not measure the soundtrack.")
    measured = json.loads(blocks[-1])
    fields = {"measured_I": "input_i", "measured_LRA": "input_lra", "measured_TP": "input_tp",
              "measured_thresh": "input_thresh", "offset": "target_offset"}
    values = {key: float(measured[source]) for key, source in fields.items()}
    if not all(math.isfinite(value) for value in values.values()):
        raise RuntimeError("Audio normalization requires an audible soundtrack; the measured audio is silent or invalid.")
    filt = target + ":" + ":".join(f"{key}={value}" for key, value in values.items()) + ":linear=true"
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=".loudnorm-", suffix=dest.suffix, dir=dest.parent, delete=False) as f:
        pending = Path(f.name)
    try:
        _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-map", "0:v:0", "-map", "0:a:0",
              "-af", filt, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
              "-movflags", "+faststart", str(pending)])
        pending.replace(dest)
    finally:
        pending.unlink(missing_ok=True)
    return dest


def encode_master(src: Path, dest: Path, w: int, h: int) -> Path:
    """Финальный профиль для Reels: H.264 high, 24fps, CRF 18, AAC 192k, faststart."""
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-vf", f"scale={w}:{h},fps=24,format=yuv420p",
          "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", "18", "-c:a", "aac", "-b:a", "192k",
          "-movflags", "+faststart", str(dest)])
    return dest


def _srt_time(t: float) -> str:
    h = int(t // 3600); m = int(t % 3600 // 60); s = int(t % 60); ms = int(round((t - int(t)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(cues: list[dict], dest: Path) -> Path:
    """cues: [{start, end, text}] в секундах абсолютного таймлайна серии."""
    out = []
    for i, c in enumerate(cues, 1):
        out.append(f"{i}\n{_srt_time(c['start'])} --> {_srt_time(c['end'])}\n{c['text']}\n")
    dest.write_text("\n".join(out), encoding="utf-8")
    return dest


def burn_subtitles(video: Path, srt: Path, dest: Path) -> Path:
    # MarginV=250 держит текст в нижней caption-safe зоне 9:16 (Reels UI перекрывает ~ нижние 300px)
    style = "FontName=Helvetica,FontSize=15,PrimaryColour=&H00FFFFFF,OutlineColour=&H90000000,Outline=1.5,Shadow=0,MarginV=250,Alignment=2"
    srt_path = str(srt.resolve()).replace("\\", "/").replace(":", "\\:")
    _run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-vf", f"subtitles='{srt_path}':force_style='{style}'", "-c:a", "copy", str(dest)])
    return dest


# ---------- automated QA ----------

def black_segments(video: Path, min_dur: float = 0.3) -> list[dict]:
    err = _run(["ffmpeg", "-loglevel", "info", "-i", str(video), "-vf", f"blackdetect=d={min_dur}:pic_th=0.98", "-an", "-f", "null", "-"])
    segs = []
    for m in re.finditer(r"black_start:(\S+) black_end:(\S+) black_duration:(\S+)", err):
        segs.append({"start": float(m.group(1)), "end": float(m.group(2)), "duration": float(m.group(3))})
    return segs


def loudness(video: Path) -> dict:
    err = _run(["ffmpeg", "-loglevel", "info", "-i", str(video), "-af", "ebur128=peak=true", "-vn", "-f", "null", "-"])
    def grab(pat):
        m = re.findall(pat, err)
        return float(m[-1]) if m else None
    return {"integrated_lufs": grab(r"I:\s+(-?[\d.]+) LUFS"), "lra": grab(r"LRA:\s+([\d.]+) LU"), "true_peak_dbtp": grab(r"Peak:\s+(-?[\d.]+) dBFS")}


def decode_check(video: Path) -> bool:
    """Полное декодирование без ошибок = нет битых кадров."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"], capture_output=True, text=True)
    return p.returncode == 0 and not p.stderr.strip()
