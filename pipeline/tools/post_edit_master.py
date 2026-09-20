#!/usr/bin/env python3
"""Make a reversible post-only editorial pass from an existing master.

No provider is called and the source file is never modified.  The caller
supplies the real scene boundaries and the final good frame.  Short visual and
audio overlaps take the mechanical edge off provider-clip joins; the result is
then retimed by the small amount necessary to keep the approved episode length.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


def parse_numbers(value: str) -> list[float]:
    return [float(item.strip()) for item in value.split(",") if item.strip()]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--cuts", required=True, help="Comma-separated scene boundaries, excluding 0 and end")
    parser.add_argument("--good-end", required=True, type=float, help="Last acceptable source timestamp")
    parser.add_argument("--target", required=True, type=float, help="Finished duration")
    parser.add_argument("--soft-after", default="", help="1-based scene numbers whose outgoing cut may dissolve")
    parser.add_argument("--soft-duration", default=0.30, type=float)
    parser.add_argument("--hard-duration", default=0.08, type=float)
    args = parser.parse_args()

    source_duration = probe_duration(args.source)
    if not 0 < args.good_end <= source_duration:
        raise SystemExit(f"good-end must be within the source ({source_duration:.3f}s)")
    cuts = parse_numbers(args.cuts)
    points = [0.0, *cuts, args.good_end]
    if points != sorted(points) or len(set(points)) != len(points):
        raise SystemExit("cuts must be unique and increasing")
    soft_after = {int(item) for item in args.soft_after.split(",") if item.strip()}
    overlaps = [args.soft_duration if scene in soft_after else args.hard_duration
                for scene in range(1, len(points) - 1)]
    segment_lengths = [end - start for start, end in zip(points, points[1:])]
    edited_duration = sum(segment_lengths) - sum(overlaps)
    if edited_duration <= 0:
        raise SystemExit("transition durations consume the edit")

    graph: list[str] = []
    video_labels: list[str] = []
    audio_labels: list[str] = []
    for index, (start, end) in enumerate(zip(points, points[1:])):
        video = f"v{index}"
        audio = f"a{index}"
        graph.append(
            f"[0:v]trim=start={start:.6f}:end={end:.6f},setpts=PTS-STARTPTS,"
            f"fps=24,format=yuv420p[{video}]"
        )
        graph.append(
            f"[0:a]atrim=start={start:.6f}:end={end:.6f},asetpts=PTS-STARTPTS,"
            f"aresample=48000,aformat=channel_layouts=stereo[{audio}]"
        )
        video_labels.append(video)
        audio_labels.append(audio)

    current_video = video_labels[0]
    current_audio = audio_labels[0]
    running = segment_lengths[0]
    for index, overlap in enumerate(overlaps, start=1):
        next_video = f"vx{index}"
        next_audio = f"ax{index}"
        offset = running - overlap
        graph.append(
            f"[{current_video}][{video_labels[index]}]xfade=transition=fade:"
            f"duration={overlap:.3f}:offset={offset:.6f}[{next_video}]"
        )
        graph.append(
            f"[{current_audio}][{audio_labels[index]}]acrossfade=d={overlap:.3f}:"
            f"c1=tri:c2=tri[{next_audio}]"
        )
        running += segment_lengths[index] - overlap
        current_video = next_video
        current_audio = next_audio

    video_factor = args.target / edited_duration
    audio_factor = edited_duration / args.target
    graph.append(
        f"[{current_video}]setpts=PTS*{video_factor:.9f},"
        f"trim=duration={args.target:.6f},setpts=PTS-STARTPTS,format=yuv420p[vout]"
    )
    graph.append(
        f"[{current_audio}]atempo={audio_factor:.9f},"
        f"atrim=duration={args.target:.6f},"
        f"afade=t=in:st=0:d=0.12,afade=t=out:st={max(0.0, args.target - 0.35):.3f}:d=0.35[aout]"
    )

    args.destination.parent.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(args.source),
        "-filter_complex", ";".join(graph), "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-profile:v", "high", "-preset", "medium", "-crf", "18",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", "-t", f"{args.target:.6f}", str(args.destination),
    ])


if __name__ == "__main__":
    main()
