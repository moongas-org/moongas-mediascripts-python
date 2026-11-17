"""Reformat track YAML collections and repair missing AcoustID recordings lists."""

import argparse
import logging
import os
import shlex
import time
from collections import Counter
from collections.abc import Iterable, Mapping, MutableMapping, Sequence, Set
from io import StringIO
from pathlib import Path
from typing import Protocol, cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedBase
from ruamel.yaml.error import YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
from ruamel.yaml.tokens import CommentToken

from mediascripts.track.track_yaml.scan_media_files_to_track_yaml import (
    TRACK_YAML_SUFFIXES,
    collection_data_path,
    load_track_yaml_files,
)
from mediascripts.track.track_yaml.track_yaml_common import (
    TextWriter,
    save_yaml_atomically,
    show_yaml_file_diff,
    track_yaml,
)

logger = logging.getLogger(__name__)


class _Yaml(Protocol):
    def scan(self, stream: str) -> Iterable["_Token"]: ...
    def compose(self, stream: str) -> Node | None: ...
    def load(self, stream: str) -> object: ...
    def dump(self, data: object, stream: StringIO) -> None: ...


class _Token(Protocol):
    comment: object


class _CommentText(Protocol):
    value: str


class _CommentedFormat(Protocol):
    def flow_style(self) -> bool | None: ...
    def set_block_style(self) -> None: ...


class _CommentedValue(Protocol):
    fa: _CommentedFormat


class _TaggedNode(Protocol):
    tag: str


class _ScalarYamlNode(_TaggedNode, Protocol):
    value: str


class _SequenceYamlNode(_TaggedNode, Protocol):
    value: list[Node]
    flow_style: bool | None


class _MappingYamlNode(_TaggedNode, Protocol):
    value: list[tuple[Node, Node]]
    flow_style: bool | None


def _set_block_style(value: object, seen: set[int]) -> bool:
    if not isinstance(value, CommentedBase) or id(value) in seen:
        return False
    seen.add(id(value))
    changed = False
    if (
        isinstance(value, (Mapping, Sequence, Set))
        and value
        and cast(_CommentedValue, value).fa.flow_style()
    ):
        cast(_CommentedValue, value).fa.set_block_style()
        changed = True
    children: Iterable[object]
    if isinstance(value, Mapping):
        children = (
            child
            for pair in cast(Mapping[object, object], value).items()
            for child in pair
        )
    elif isinstance(value, (Sequence, Set)):
        children = iter(cast(Iterable[object], value))
    else:
        return changed
    for child in children:
        changed = _set_block_style(child, seen) or changed
    return changed


def _same_yaml_nodes(
    original: Node | None,
    updated: Node | None,
    seen: dict[int, int],
    reverse: dict[int, int],
) -> bool:
    if original is None or updated is None:
        return original is updated
    if (
        type(original) is not type(updated)
        or cast(_TaggedNode, original).tag != cast(_TaggedNode, updated).tag
    ):
        return False
    if isinstance(original, ScalarNode) and isinstance(updated, ScalarNode):
        original_scalar = cast(_ScalarYamlNode, original)
        updated_scalar = cast(_ScalarYamlNode, updated)
        if original_scalar.tag == "tag:yaml.org,2002:null":
            return True
        return original_scalar.value == updated_scalar.value
    if id(original) in seen or id(updated) in reverse:
        return seen.get(id(original)) == id(updated) and reverse.get(id(updated)) == id(
            original
        )
    seen[id(original)] = id(updated)
    reverse[id(updated)] = id(original)
    if isinstance(original, SequenceNode) and isinstance(updated, SequenceNode):
        original_sequence = cast(_SequenceYamlNode, original)
        updated_sequence = cast(_SequenceYamlNode, updated)
        return len(original_sequence.value) == len(updated_sequence.value) and all(
            _same_yaml_nodes(a, b, seen, reverse)
            for a, b in zip(original_sequence.value, updated_sequence.value)
        )
    if isinstance(original, MappingNode) and isinstance(updated, MappingNode):
        original_mapping = cast(_MappingYamlNode, original)
        updated_mapping = cast(_MappingYamlNode, updated)
        return len(original_mapping.value) == len(updated_mapping.value) and all(
            _same_yaml_nodes(ak, bk, seen, reverse)
            and _same_yaml_nodes(av, bv, seen, reverse)
            for (ak, av), (bk, bv) in zip(original_mapping.value, updated_mapping.value)
        )
    return False


def _has_flow_collections(node: Node | None, seen: set[int]) -> bool:
    if node is None or id(node) in seen:
        return False
    seen.add(id(node))
    if isinstance(node, SequenceNode):
        sequence = cast(_SequenceYamlNode, node)
        return bool(sequence.value and sequence.flow_style) or any(
            _has_flow_collections(child, seen) for child in sequence.value
        )
    if isinstance(node, MappingNode):
        mapping = cast(_MappingYamlNode, node)
        return bool(mapping.value and mapping.flow_style) or any(
            _has_flow_collections(key, seen) or _has_flow_collections(value, seen)
            for key, value in mapping.value
        )
    return False


def _comments(content: str) -> Counter[str]:
    comments: Counter[str] = Counter()

    def collect(value: object) -> None:
        if isinstance(value, CommentToken):
            for line in cast(_CommentText, value).value.splitlines():
                if line.strip().startswith("#"):
                    comments[line.strip()] += 1
        elif isinstance(value, (list, tuple)):
            for item in cast(Sequence[object], value):
                collect(item)

    for token in cast(_Yaml, YAML()).scan(content):
        collect(token.comment)
    return comments


def _repair_acoustid_recordings(data: object) -> bool:
    if not isinstance(data, Mapping):
        return False
    document = cast(Mapping[str, object], data)
    changed = False
    for key in ("mediaFileData", "media_file_data", "mediafile_data"):
        entries = document.get(key)
        if not isinstance(entries, list):
            continue
        for entry in cast(list[object], entries):
            if not isinstance(entry, Mapping):
                continue
            media_file = cast(Mapping[str, object], entry)
            for acoustid_key in ("acoustidData", "acoustid_data"):
                acoustid = media_file.get(acoustid_key)
                if not isinstance(acoustid, Mapping):
                    continue
                results = cast(Mapping[str, object], acoustid).get("results")
                if not isinstance(results, list):
                    continue
                for result in cast(list[object], results):
                    if isinstance(result, MutableMapping) and "recordings" not in result:
                        cast(MutableMapping[str, object], result)["recordings"] = []
                        changed = True
    return changed


def _expected_acoustid_repairs(node: Node | None) -> None:
    """Allow only the targeted recordings additions in the semantic comparison."""
    def field(mapping: Node | None, names: tuple[str, ...]) -> Iterable[Node]:
        if isinstance(mapping, MappingNode):
            for key, value in cast(_MappingYamlNode, mapping).value:
                if isinstance(key, ScalarNode) and cast(_ScalarYamlNode, key).value in names:
                    yield value

    def items(sequence: Node) -> Iterable[Node]:
        if isinstance(sequence, SequenceNode):
            yield from cast(_SequenceYamlNode, sequence).value

    for entries in field(node, ("mediaFileData", "media_file_data", "mediafile_data")):
        for entry in items(entries):
            for acoustid in field(entry, ("acoustidData", "acoustid_data")):
                for results in field(acoustid, ("results",)):
                    for result in items(results):
                        if isinstance(result, MappingNode) and not any(field(result, ("recordings",))):
                            cast(_MappingYamlNode, result).value.append((
                                ScalarNode("tag:yaml.org,2002:str", "recordings"),
                                SequenceNode("tag:yaml.org,2002:seq", [], flow_style=True),
                            ))


def reformat_track_yaml(path: Path, dry_run: bool = False) -> bool:
    original = path.read_text(encoding="utf-8")
    yaml = track_yaml(preserve_quotes=True)
    typed_yaml = cast(_Yaml, yaml)
    data = typed_yaml.load(original)
    repaired = _repair_acoustid_recordings(data)
    reformatted = _set_block_style(data, set())
    if not repaired and not reformatted:
        return False
    original_node = cast(_Yaml, YAML()).compose(original)
    if repaired:
        _expected_acoustid_repairs(original_node)
    output = StringIO()
    typed_yaml.dump(data, output)
    proposed = output.getvalue()
    proposed_node = cast(_Yaml, YAML()).compose(proposed)
    if not _same_yaml_nodes(original_node, proposed_node, {}, {}):
        raise ValueError("YAML content or aliases would change during reformatting")
    if _has_flow_collections(proposed_node, set()):
        raise ValueError("YAML still contains non-empty flow-style collections")
    if _comments(original) != _comments(proposed):
        raise ValueError("YAML comments would change during reformatting")
    if dry_run:
        show_yaml_file_diff(path, original, proposed)
    else:

        def write(stream: TextWriter) -> None:
            stream.write(proposed)

        yaml_path = path.resolve()
        backup_path = yaml_path.with_name(f"{yaml_path.name}.bak")
        save_yaml_atomically(yaml_path, write, backup_path)
        logger.info("\n%s\n", shlex.join(["meld", str(backup_path), str(yaml_path)]))
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument(
        "--file", type=Path, help="Process one existing track YAML file."
    )
    scope.add_argument(
        "-d",
        "--dir",
        type=Path,
        help="Recursively scan a directory instead of the collection.",
    )
    parser.add_argument(
        "-c", "--count", type=int, help="Update at most this many track YAML files (would update in dry-run)."
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Show changes without writing files.",
    )
    args = parser.parse_args()
    if args.count is not None and args.count < 0:
        parser.error("--count must be non-negative")
    logging.basicConfig(
        level=getattr(
            logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO
        )
    )
    try:
        if args.file is not None:
            if not args.file.is_file():
                raise ValueError(f"Specified file does not exist: {args.file}")
            if args.file.suffix.lower() not in TRACK_YAML_SUFFIXES:
                raise ValueError(
                    f"Specified file is not a YAML track file: {args.file}"
                )
            files = [args.file]
        else:
            directory = collection_data_path(args.dir)
            logger.info("Scanning for track YAML files in %s", directory)
            files = load_track_yaml_files(
                directory, album_directories_only=args.dir is None
            )
        logger.info("Found %d track YAML file(s)%s", len(files), " (dry-run)" if args.dry_run else "")
        updated = 0
        failed = 0
        processed = 0
        last_progress: float | None = None
        for path in files:
            if args.count is not None and updated >= args.count:
                break
            now = time.monotonic()
            if last_progress is None or now - last_progress >= 5:
                logger.info(
                    "Processing file %d/%d; %d %s, %d failed: %s",
                    processed + 1,
                    len(files),
                    updated,
                    "would change" if args.dry_run else "updated",
                    failed,
                    path,
                )
                last_progress = now
            processed += 1
            try:
                if reformat_track_yaml(path, dry_run=args.dry_run):
                    if args.dry_run:
                        logger.info("Would update %s", path)
                    updated += 1
            except (OSError, UnicodeError, YAMLError, ValueError, TypeError) as error:
                logger.warning("Could not reformat %s: %s", path, error)
                failed += 1
        logger.info(
            "Processed %d track YAML file(s); %d %s, %d failed",
            processed,
            updated,
            "would change" if args.dry_run else "updated",
            failed,
        )
        return 1 if failed else 0
    except KeyboardInterrupt:
        logger.info("Interrupted by user; exiting")
        return 130
    except (OSError, ValueError) as error:
        logger.error("%s", error)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
