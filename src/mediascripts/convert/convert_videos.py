import argparse
import os
import subprocess
from pathlib import Path

"""
Script to batch convert video from one format (e.g. wmv) to mp4 (x264) (in place),
optionally deleting the source video files.
"""


def convert_all(
    source_root: Path,
    source_ext: str = ".wmv",
    dest_ext: str = ".h264.mp4",
    exclude_keywords: list[str] = [],
    delete_source_file: bool = True,
    overwrite: bool = False,
    dry_run: bool = False,
) -> int:
    """
    Recursively batch convert images from one format to another,
    with the specified resolution and quality settings.
    The conversion is performed in place.

    Returns the number it converted (or would have converted if not dry_run)

    """

    starting_directory = os.getcwd()
    count = 0
    for root, _, files in os.walk(source_root, topdown=False):
        for fname in files:
            for keyword in exclude_keywords:
                if root.find(keyword) != -1:
                    continue
            if fname.endswith(source_ext):
                source_base, source_ext = os.path.splitext(fname)
                dest_filename = source_base + dest_ext
                dest_path = Path(root).joinpath(dest_filename)
                cmd = f"ffmpeg -i '{fname}' -c:v libx264 -c:a aac '{dest_filename}'"
                print(f"root={root} cmd={cmd}")
                os.chdir(root)
                if overwrite or not dest_path.exists():
                    if not dry_run:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-i",
                                fname,
                                "-c:v",
                                "libx264",
                                "-c:a",
                                "aac",
                                dest_filename,
                            ],
                            check=False,
                        )
                if delete_source_file:
                    os.remove(fname)
                os.chdir(starting_directory)
                count += 1
    return count


def main():
    parser = argparse.ArgumentParser(
        description="Batch converts videos from one format (e.g. wmv) to mp4 (x264) in place."
    )
    parser.add_argument(
        "--source-root",
        default="/videos",
        help="Root directory to recursively search for source videos (default: /videos)",
    )
    parser.add_argument(
        "--source-ext",
        default=".wmv",
        help="Source file extension to convert (default: .wmv)",
    )
    parser.add_argument(
        "--output-fmt",
        dest="output_fmts",
        action="append",
        help="Destination format(s), e.g. h264.mp4. Repeat for multiple formats (default: h264.mp4)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite existing destination files",
    )
    parser.add_argument(
        "--keep-source-files",
        action="store_true",
        help="Do not delete source files after conversion",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be converted without converting",
    )
    args = parser.parse_args()

    output_fmts = args.output_fmts if args.output_fmts else ["h264.mp4"]

    for dest_fmt in output_fmts:
        # recommend a fixed-height resolution with a variable width
        # consider square flags like Switzerland
        # if we matched the width, it would look out of proportion
        source_path = Path(f"{args.source_root}/")
        dest_ext = f".{dest_fmt}"
        # convert in place folder), then delete original
        count = convert_all(
            source_root=source_path.resolve(),
            source_ext=args.source_ext,
            dest_ext=dest_ext,
            delete_source_file=not args.keep_source_files,
            overwrite=args.overwrite,
            dry_run=args.dry_run,
        )
        print(f"converted {count} videos")


if __name__ == "__main__":

    main()
