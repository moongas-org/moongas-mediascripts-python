#!/bin/bash

import argparse
import importlib.metadata
import logging
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def create_script_symlinks(dest_dir: Path):
    # Resolve the scripts directory from this file's location
    scripts_dir = Path(__file__).resolve().parent
    shell_dir = scripts_dir / "shell"

    # Symlink every media* console script entry point (except this script itself)
    eps = importlib.metadata.entry_points(group="console_scripts")
    for ep in eps:
        package_name = ep.dist.name if ep.dist else "Unknown"
        if not package_name.startswith("media"):
            continue
        if ep.name == "mediascripts":
            continue
        command_path = shutil.which(ep.name)
        if command_path is None:
            logger.warning("%s not found on PATH, skipping", ep.name)
            continue
        dest = dest_dir / ep.name
        if dest.is_symlink():
            dest.unlink()
            logger.info("removed existing symlink %s", dest.name)
        elif dest.exists():
            logger.warning(
                "skipping %s (destination already exists and is not a symlink)",
                dest.name,
            )
            continue
        dest.symlink_to(command_path)
        logger.info("%s -> %s", dest.name, command_path)

    # Symlink every shell script in scripts/shell with hyphenated name
    if shell_dir.is_dir():
        for shell_script in sorted(shell_dir.glob("*.sh")):
            name = shell_script.stem.replace("_", "-")
            dest = dest_dir / name
            if dest.is_symlink():
                dest.unlink()
                logger.info("removed existing symlink %s", dest.name)
            elif dest.exists():
                logger.warning(
                    "skipping %s (destination already exists and is not a symlink)",
                    dest.name,
                )
                continue
            dest.symlink_to(shell_script)
            logger.info("%s -> %s", dest.name, shell_script)
    else:
        logger.warning("shell script directory not found: %s", shell_dir)


def list_media_scripts():
    # Fetch all entry points belonging to the 'console_scripts' group
    eps = importlib.metadata.entry_points(group="console_scripts")

    # Group command names by the scripts subdirectory of the target module,
    # e.g. "scripts.convert.convert_covers:main" belongs to the "convert" group
    groups: dict[str, list[str]] = {}
    for ep in eps:
        # ep.value holds the 'module:function' path
        # ep.dist holds metadata about the package providing it (if available)
        package_name = ep.dist.name if ep.dist else "Unknown"
        if not package_name.startswith("media"):
            continue
        parts = ep.value.split(":")[0].split(".")
        group = parts[1] if len(parts) > 2 else "(root)"
        if ep.name != "mediascripts":
            groups.setdefault(group, []).append(ep.name)

    for group in sorted(groups):
        print(f"{group}:")
        for name in sorted(groups[group]):
            print(f"  {name}")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    parser = argparse.ArgumentParser(
        description="Utility for media* console script entry points."
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list all media* console script entry points, grouped by scripts subdirectory",
    )
    parser.add_argument(
        "--create-script-symlinks",
        action="store_true",
        help="create hyphenated symlinks in the current directory to every console entry script "
        "and every shell script in scripts/shell",
    )
    args = parser.parse_args()
    if args.list:
        list_media_scripts()
    elif args.create_script_symlinks:
        create_script_symlinks(Path.cwd())
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
