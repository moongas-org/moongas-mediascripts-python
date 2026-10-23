"""Compare sibling <track>.yml metadata with MP3 ID3 tags without changing files.

Walk album directories under $MOONGAS_COLLECTION_ROOTDIR/data, following library
symlinks. Compare the six core trackData fields; streaming identifiers and extra
ID3 tags are outside this comparison. ID3 dates are compared by year, and leading
or trailing whitespace is ignored. Quiet mode suppresses matching-track messages.
Tracks whose artist directory or YAML artist contains a newline-separated keyword from
COPY_MEDIALIB_EXCLUDE_KEYWORDS are ignored (case-sensitive; blank lines ignored).

Use -d / --dir to recursively scan an arbitrary directory instead, without
requiring MOONGAS_COLLECTION_ROOTDIR or a collection directory structure.

Usage: compare-track-yaml-with-media-file [-q | --quiet] [-d DIRECTORY]
Exit status: 0 when all tracks match, 1 on differences or errors.
"""

import logging
from pathlib import Path

from mediascripts.track.track_yaml.track_yaml_common import (
    TAG_FIELDS,
    TRACK_ERRORS,
    load_track_metadata,
    run_cli,
    tag_values,
)

logger = logging.getLogger(__name__)


def compare_track(track_path: Path, quiet: bool = False) -> bool:
    """Report differences and return whether all core fields match."""
    yaml_path = track_path.with_suffix(".yml")
    try:
        metadata = load_track_metadata(track_path)
    except TRACK_ERRORS as error:
        logger.error("[%s] Could not compare %s: %s", track_path, yaml_path, error)
        return False
    if metadata is None:
        return True
    track_data, tags = metadata

    matches = True
    for field, tag in TAG_FIELDS.items():
        expected = str(getattr(track_data, field)).strip()
        actual = tag_values(tags, field)
        if actual != [expected]:
            matches = False
            logger.warning(
                "[%s] %s mismatch: YAML=%r, ID3 %s=%r",
                track_path, field, expected, tag, actual,
            )
    if matches and not quiet:
        logger.info("[%s] Track YAML matches media file tags", track_path)
    return matches


def main() -> int:
    return run_cli(__doc__, compare_track)


if __name__ == "__main__":
    raise SystemExit(main())
