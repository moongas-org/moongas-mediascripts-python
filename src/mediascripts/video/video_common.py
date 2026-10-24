"""Shared video probing, argument validation, and ffmpeg concat helpers."""

import argparse
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

# Container formats ffprobe reports for still images rather than videos.
IMAGE_FORMAT_RE = re.compile(r"^(image2|.*_pipe|tty|bin)$")


@dataclass(frozen=True)
class VideoInfo:
    path: Path
    duration: float
    has_audio: bool


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def parse_sexagesimal(value: str) -> float:
    """Convert ffprobe's H:MM:SS.ffffff duration into seconds."""
    hours, minutes, seconds = value.strip().split(":")
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def probe_video(path: Path) -> VideoInfo | None:
    """Return duration/audio info if ffprobe recognises `path` as a video, else None."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-sexagesimal",
            "-show_entries",
            "format=duration,format_name:stream=codec_type:stream_disposition=attached_pic",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        data = cast(dict[str, Any], json.loads(result.stdout))
    except json.JSONDecodeError:
        return None

    fmt = cast(dict[str, Any], data.get("format", {}))
    streams = cast(list[dict[str, Any]], data.get("streams", []))
    if IMAGE_FORMAT_RE.match(str(fmt.get("format_name", ""))):
        return None

    has_video = any(
        s.get("codec_type") == "video"
        and not cast(dict[str, int], s.get("disposition", {})).get("attached_pic", 0)
        for s in streams
    )
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration_text = str(fmt.get("duration", ""))
    if not has_video or ":" not in duration_text:
        return None

    duration = parse_sexagesimal(duration_text)
    if duration <= 0:
        return None
    return VideoInfo(path=path, duration=duration, has_audio=has_audio)


def concat_line(name: str) -> str:
    return "file '" + name.replace("'", "'\\''") + "'\n"


def write_concat_script(output_dir: Path) -> Path:
    script = output_dir / "concat.sh"
    script.write_text(
        "#!/bin/bash\n"
        "ffmpeg -f concat -safe 0 -i inputs.txt -c copy output.mp4\n"
    )
    try:
        mode = script.stat().st_mode
        # Grant execute wherever read is already granted (like chmod +x under umask).
        script.chmod(mode | ((mode & 0o444) >> 2))
    except OSError as exc:
        print(f"  note: could not make {script.name} executable ({exc.strerror})")
    return script
