import argparse
import os
from pathlib import Path
import subprocess

from mediascan.utils.log import log_arguments
from mediascan.utils.path import get_size_human_readable

"""
Script to batch convert covers from jpg to webp (in place),
optionally deleting the src jpegs.
By default it converts at 80% quality and 1000x1000 resolution.

To check the progress, in a separate terminal you can do this:

This number should be going up:

$ find /data/Covers -name "*.webp" | wc -l
533

This number should be going down:

$ find /data/Covers -name "*.jpg" | wc -l
4068

Before:

$ du -shc /data/Covers
1.3G	/data/Covers
1.3G	total

After:

$ du -shc /data/Covers
439M	/data/Covers
439M	total

Etc.

Typical workflow to update covers on my server:

$ python scripts/copy_covers.py
$ python scripts/convert_covers.py
$ rsync -ahvP /data/Covers/ root@$DROPLET_IP:/var/www/html/Covers/ --delete

"""

@log_arguments
def convert_medialib_cover_images_inplace(
    src_path: Path,
    src_fname: str = "cover.jpg",
    dst_fname: str = "cover.webp",
    exclude_keywords: list[str] = [],
    resolution: str = "1000x1000",
    quality: int = 80,
    dry_run: bool = False,
    delete_src_file: bool = True,
    overwrite: bool = False,
) -> int:
    """
    Recursively batch convert images (in place) from one format to another,
    with the specified resolution and quality settings.

    Returns the number it converted (or would have converted if not dry_run)

    src_path: path to medialib e.g. '/data/Music'
    src_fname: the source filename to match e.g. 'cover.jpg'
    dst_fname: the destination filename to convert to e.g. 'cover.webp'
    delete_src_file: whether or not to delete source file after conversion (default=true)
    """
    count = 0
    for root, _, files in os.walk(src_path, topdown=False):
        for fname in files:
            src_file_abs_path: Path = Path(root).joinpath(fname)
            dst_file_abs_path: Path = Path(root).joinpath(dst_fname)
            skip = False
            for keyword in exclude_keywords:
                if str(src_file_abs_path).find(keyword) != -1:
                    print(f"Skipping file '{src_file_abs_path}' based on exclude keyword '{keyword}'")
                    skip = True
            if skip:
                continue
            if fname == src_fname:
                if dst_file_abs_path.exists() and not overwrite:
                    print(f"Skipping file '{src_file_abs_path}' because destination file '{dst_file_abs_path}' exists "
                        "and `--overwrite` was not specified")
                else:
                    if not dry_run:
                        cmd = [
                            "convert",
                            src_fname,
                            "-resize",
                            resolution,
                            "-quality",
                            str(quality),
                            dst_fname,
                        ]
                        print(f"Convert cmd={cmd} cwd={root}")
                        subprocess.run(cmd, cwd=root, check=True)
                        if delete_src_file:
                            os.remove(src_file_abs_path)
                            print(f"Removed source file '{src_file_abs_path}'")
                    count += 1
    return count


@log_arguments
def convert_medialibs_cover_images_inplace(
    src_paths: list[Path],
    src_filename: str = "cover.jpg",
    dst_filename: str = "cover.webp",
    exclude_keywords: list[str] = [],
    resolution: str = "1000x1000",
    quality: int = 80,
    dry_run: bool = False,
    delete_src_file: bool = True,
    overwrite: bool = False,
) -> int:
    print("Converting cover images (in-place)")

    grand_total: int = 0
    for src_path in src_paths:
        # Assuming convert_medialib_cover_images_inplace is defined elsewhere in your module
        count = convert_medialib_cover_images_inplace(
            src_path,
            src_filename,
            dst_filename,
            exclude_keywords=exclude_keywords,
            resolution=resolution,
            quality=quality,
            dry_run=dry_run,
            delete_src_file=delete_src_file,
            overwrite=overwrite,
        )
        print(f"Total converted for medialib dir {src_path}: {count}")
        grand_total += count

    print(f"Grand total converted for all medialib dirs: {grand_total}")
    return grand_total


def make_archive(src_path: Path, dst_path: Path) -> None:
    """
    src_path: path to the source directory to compress
    dst_path: path to the .tgz file to output
    """
    src_path = src_path.resolve()
    dst_path = dst_path.resolve()

    if dst_path.exists():
        print(f"Removing existing archive {dst_path}")
        os.remove(dst_path)

    print(f"Creating archive '{dst_path}' from '{src_path.name}'...")
    
    # Run tar inside the parent directory of src_path
    subprocess.run(
        ["tar", "czf", str(dst_path), src_path.name],
        cwd=src_path.parent,
        check=True,
    )
    print(f"Created archive: {dst_path} (size: {get_size_human_readable(dst_path)})")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert medialib cover images in-place and create a tar archive."
    )

    # Conversion arguments
    parser.add_argument(
        "-s", "--src-paths",
        type=Path,
        nargs="+",
        default=[Path("/data/Covers/Music"), Path("/data/Covers/MusicOther")],
        help="Source directory paths for image conversion."
    )
    parser.add_argument(
        "--src-filename",
        default="cover.jpg",
        help="Target source filename to look for (default: cover.jpg)."
    )
    parser.add_argument(
        "--dst-filename",
        default="cover.webp",
        help="Target output filename format (default: cover.webp)."
    )
    parser.add_argument(
        "-e", "--exclude-keywords",
        nargs="*",
        default=[],
        help="Keywords to exclude matching paths."
    )
    parser.add_argument(
        "--resolution",
        default="1000x1000",
        help="Target image resolution WxH (default: 1000x1000)."
    )
    parser.add_argument(
        "--quality",
        type=int,
        default=80,
        help="Image quality setting 1-100 (default: 80)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Perform a dry run without modifying files."
    )
    parser.add_argument(
        "--keep-src-file",
        dest="delete_src_file",
        action="store_false",
        default=True,
        help="Do not delete source files after conversion (default: delete original)."
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        default=False,
        help="Overwrite target file if it already exists."
    )

    # Archive arguments
    parser.add_argument(
        "--archive-src",
        type=Path,
        default=Path("/data/Covers/"),
        help="Source path to archive (default: /data/Covers/)."
    )
    parser.add_argument(
        "--archive-dst",
        type=Path,
        default=Path("/data/Covers.tar.gz"),
        help="Destination tarball file path (default: /data/Covers.tar.gz)."
    )
    parser.add_argument(
        "--skip-archive",
        action="store_true",
        help="Skip creation of the tar archive after conversion."
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    count_converted: int = convert_medialibs_cover_images_inplace(
        src_paths=args.src_paths,
        src_filename=args.src_filename,
        dst_filename=args.dst_filename,
        exclude_keywords=args.exclude_keywords,
        resolution=args.resolution,
        quality=args.quality,
        dry_run=args.dry_run,
        delete_src_file=args.delete_src_file,
        overwrite=args.overwrite,
    )

    if count_converted == 0:
        print("Skipping archive creation because no files were converted") 
    elif args.skip_archive:
        print("Skipping archive creation because --skip-archive was specified") 
    else:
        make_archive(src_path=args.archive_src, dst_path=args.archive_dst)


if __name__ == "__main__":
    main()