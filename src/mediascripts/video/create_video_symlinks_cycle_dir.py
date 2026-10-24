#!/usr/bin/env python3
"""Create n sequentially numbered symlinks by cycling through a directory's videos.

Example use case: as an A/V clip sequencer, taking an input dir of video slices of
a set duration e.g. 1.714 seconds (140 bpm), and generating symlinks that can be
concatenated in order to create a continuous video sequence of a repeated loop.

Sources are detected with ffprobe and sorted by filename (alphanumeric order).
Only files directly in the input directory are scanned. Links are named
NNNN_SOURCE<ext>, preserving the source filename, and an ffmpeg concat list
(inputs.txt) referencing them is written to the output directory.
A concat.sh helper is also written; run it from the output directory:

    cd OUTPUT_DIR && ./concat.sh    # produces output.mp4

Unlike re-encoded slices, symlinked videos must already have compatible
codecs and stream parameters for concatenation with stream copying.
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

from mediascripts.video.video_common import (
    concat_line,
    positive_int,
    probe_video,
    write_concat_script,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "-i",
        "--input-dir",
        required=True,
        type=Path,
        help="Directory containing input video files (requires ffprobe)",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        "--output",
        required=True,
        type=Path,
        help="Directory for symlinks, inputs.txt and concat.sh (created if missing)",
    )
    parser.add_argument(
        "-n",
        "--count",
        required=True,
        type=positive_int,
        help="Total number of symlinks to create, cycling through sorted videos",
    )
    parser.add_argument(
        "-P",
        "--absolute-names",
        action="store_true",
        help="Use absolute symlink targets instead of relative ones",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_dir: Path = args.input_dir
    output_dir: Path = args.output_dir
    count: int = args.count
    absolute_names: bool = args.absolute_names

    if shutil.which("ffprobe") is None:
        sys.exit("error: 'ffprobe' not found on PATH")
    if not input_dir.is_dir():
        sys.exit(f"error: input directory '{input_dir}' does not exist")

    input_dir = input_dir.resolve()
    output_dir = output_dir.resolve()
    if input_dir == output_dir:
        sys.exit("error: input and output directories must differ")

    print(f"Probing files in {input_dir} ...")
    sources = [
        path
        for path in sorted(input_dir.iterdir())
        if path.is_file() and probe_video(path) is not None
    ]
    if not sources:
        sys.exit("error: no video files found in input directory")

    output_dir.mkdir(parents=True, exist_ok=True)

    width = max(4, len(str(count)))
    names: list[str] = []

    for i in range(1, count + 1):
        src = sources[(i - 1) % len(sources)]
        link = output_dir / f"{i:0{width}d}_{src.name}"
        target = src if absolute_names else Path(os.path.relpath(src, output_dir))

        if link.is_symlink() or link.exists():
            if not link.is_symlink():
                sys.exit(
                    f"error: '{link}' exists and is not a symlink; refusing to overwrite"
                )
            link.unlink()
        link.symlink_to(target)
        names.append(link.name)

    concat_file = output_dir / "inputs.txt"
    concat_file.write_text("".join(concat_line(name) for name in names))
    concat_script = write_concat_script(output_dir)
    print(f"Wrote {len(names)} symlinks, {concat_file} and {concat_script}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
