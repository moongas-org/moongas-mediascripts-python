#!/usr/bin/env python3
"""Extract random fixed-length slices from every video in a directory.

Slice start points are drawn uniformly over the combined timeline of all
inputs, so each video is sampled in proportion to its duration. Slices are
re-encoded to a common format (H.264/AAC, fixed size and frame rate) so the
generated ffmpeg concat list (inputs.txt) can be joined without problems.
A concat.sh helper is also written; run it from the output directory:

    cd OUTPUT_DIR && ./concat.sh    # produces output.mp4
"""

import argparse
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from mediascripts.video.video_common import (
    VideoInfo,
    concat_line,
    positive_int,
    probe_video,
    write_concat_script,
)


@dataclass(frozen=True)
class Slice:
    index: int
    video: VideoInfo
    start: float


def parse_size(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"(\d+)[xX](\d+)", value)
    if not match:
        raise argparse.ArgumentTypeError(
            f"invalid size '{value}', expected WIDTHxHEIGHT"
        )
    width, height = int(match.group(1)), int(match.group(2))
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise argparse.ArgumentTypeError(
            "width and height must be positive even numbers"
        )
    return width, height


def positive_float(value: str) -> float:
    number = float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        required=True,
        type=Path,
        help="Directory containing input video files",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        "--output",
        required=True,
        type=Path,
        help="Directory for slices, inputs.txt and concat.sh (created if missing)",
    )
    parser.add_argument(
        "-n",
        "--count",
        required=True,
        type=positive_int,
        help="Number of random slices to extract",
    )
    parser.add_argument(
        "-d",
        "--duration",
        required=True,
        type=positive_float,
        help="Duration of each slice in seconds (decimals allowed)",
    )
    parser.add_argument(
        "--size",
        type=parse_size,
        default=(1280, 720),
        help="Output size WIDTHxHEIGHT, letterboxed (default: 1280x720)",
    )
    parser.add_argument(
        "--fps",
        type=positive_float,
        default=30.0,
        help="Output frame rate (default: 30)",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Random seed for reproducible selections"
    )
    return parser.parse_args()


def format_hhmmss(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}{total % 3600 // 60:02d}{total % 60:02d}"


def choose_slices(
    videos: list[VideoInfo], count: int, slice_len: float, rng: random.Random
) -> list[Slice]:
    # Each video contributes (duration - slice_len) seconds of valid start points,
    # so weighting by that span gives every valid start point equal probability.
    spans = [v.duration - slice_len for v in videos]
    weights = spans if sum(spans) > 0 else [1.0] * len(videos)
    slices: list[Slice] = []
    for index in range(1, count + 1):
        (video,) = rng.choices(videos, weights=weights)
        start = rng.uniform(0, max(0.0, video.duration - slice_len))
        slices.append(Slice(index=index, video=video, start=start))
    return slices


def extract_slice(
    sl: Slice, slice_len: float, out_path: Path, size: tuple[int, int], fps: float
) -> None:
    width, height = size
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p"
    )
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{sl.start:.3f}",
        "-i",
        str(sl.video.path),
    ]
    if sl.video.has_audio:
        audio_map = "0:a:0"
    else:
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_map = "1:a:0"
    cmd += [
        "-t",
        f"{slice_len:.3f}",
        "-map",
        "0:v:0",
        "-map",
        audio_map,
        "-vf",
        vf,
        "-af",
        "apad",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-ac",
        "2",
        "-movflags",
        "+faststart",
        str(out_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed for {out_path.name}:\n{result.stderr.strip()}"
        )


def main() -> int:
    args = parse_args()
    input_dir: Path = args.input_dir
    output_dir: Path = args.output_dir
    count: int = args.count
    slice_len: float = args.duration
    size: tuple[int, int] = args.size
    fps: float = args.fps
    seed: int | None = args.seed

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            sys.exit(f"error: '{tool}' not found on PATH")
    if not input_dir.is_dir():
        sys.exit(f"error: input directory '{input_dir}' does not exist")
    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if input_dir == output_dir:
        sys.exit("error: input and output directories must differ")

    print(f"Probing files in {input_dir} ...")
    durations: dict[Path, VideoInfo] = {}
    for path in sorted(p for p in input_dir.iterdir() if p.is_file()):
        info = probe_video(path)
        if info is None:
            continue
        durations[path] = info
        print(f"  {path.name}: {info.duration:.3f}s")

    if not durations:
        sys.exit("error: no video files found in input directory")

    usable = [v for v in durations.values() if v.duration >= slice_len]
    for v in durations.values():
        if v.duration < slice_len:
            print(f"  skipping {v.path.name}: shorter than {slice_len}s")
    if not usable:
        sys.exit(f"error: no videos are at least {slice_len}s long")

    rng = random.Random(seed)
    slices = choose_slices(usable, count, slice_len, rng)

    output_dir.mkdir(parents=True, exist_ok=True)
    width = max(4, len(str(count)))
    names: list[str] = []
    for sl in slices:
        name = (
            f"{sl.index:0{width}d}_{sl.video.path.stem}_{format_hhmmss(sl.start)}.mp4"
        )
        print(f"[{sl.index}/{count}] {name} (start {sl.start:.3f}s)")
        try:
            extract_slice(sl, slice_len, output_dir / name, size, fps)
        except RuntimeError as exc:
            sys.exit(f"error: {exc}")
        names.append(name)

    concat_file = output_dir / "inputs.txt"
    concat_file.write_text("".join(concat_line(name) for name in names))
    concat_script = write_concat_script(output_dir)
    print(f"Wrote {len(names)} slices, {concat_file} and {concat_script}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
