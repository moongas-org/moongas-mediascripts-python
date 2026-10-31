"""Load recoverable track YAML and serialize independent ISRC lists."""

import logging
from copy import deepcopy
from pathlib import Path
from typing import Protocol, TypeGuard, cast

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedSeq
from ruamel.yaml.error import YAMLError

from mediascripts.track.track_yaml.track_yaml_common import TextWriter, save_yaml_atomically

logger = logging.getLogger(__name__)


class _YamlLoader(Protocol):
    def load(self, stream: str) -> object: ...


class _Anchor(Protocol):
    value: str | None


class _SequenceStyle(Protocol):
    anchor: _Anchor

    def yaml_set_anchor(self, value: str | None) -> None: ...


def is_mapping(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict) and all(isinstance(key, str) for key in cast(dict[object, object], value))


def is_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


def load_track(path: Path, *, dry_run: bool) -> tuple[str, object, bool]:
    original = path.read_text(encoding="utf-8")
    yaml = YAML()
    try:
        return original, cast(_YamlLoader, yaml).load(original), False
    except YAMLError as error:
        backup = path.with_name(path.name + ".bak")
        if not backup.is_file():
            raise
        restored = backup.read_text(encoding="utf-8")
        backup_yaml = YAML()
        document = cast(_YamlLoader, backup_yaml).load(restored)
        if not is_mapping(document) or not is_mapping(document.get("trackData")):
            raise ValueError(f"Backup is not a track YAML document: {backup}") from error
        logger.warning("[%s] Unparseable YAML; recovering from %s", path, backup)
        if not dry_run:

            def write(output: TextWriter) -> None:
                output.write(restored)

            if path.read_text(encoding="utf-8") != original:
                raise ValueError("Track YAML changed during backup recovery; refusing to overwrite")
            save_yaml_atomically(path, write)
            original = restored
        return original, document, True


def normalize_isrcs(document: object) -> bool:
    """Detach aliases and clear invalid ISRC values for re-lookup."""
    if not is_mapping(document):
        return False
    changed = False
    seen_lists: set[int] = set()
    for collection_key in ("trackIdData", "mediaFileData"):
        collection = document.get(collection_key)
        if collection_key == "trackIdData" and is_mapping(collection):
            entries = [collection]
        elif is_list(collection):
            entries = collection
        else:
            continue
        for entry in entries:
            if not is_mapping(entry):
                continue
            data = entry.get("acoustidData")
            if not is_mapping(data) or not is_list(results := data.get("results")):
                continue
            for result in results:
                if not is_mapping(result) or not is_list(recordings := result.get("recordings")):
                    continue
                for recording in recordings:
                    if not is_mapping(recording):
                        continue
                    if "isrcs" not in recording:
                        continue
                    value = recording.get("isrcs")
                    valid = is_list(value) and all(isinstance(item, str) and item.strip() for item in value)
                    if not valid:
                        logger.warning(
                            "Recording %r: invalid or missing isrcs %r; resetting to [] for lookup",
                            recording.get("id"),
                            value,
                        )
                    items = value if valid and is_list(value) else []
                    shared = is_list(value) and id(value) in seen_lists
                    if is_list(value):
                        seen_lists.add(id(value))
                    normalized = deepcopy(value) if valid and isinstance(value, CommentedSeq) else CommentedSeq(items)
                    style = cast(_SequenceStyle, normalized)
                    style.yaml_set_anchor(None)
                    if (
                        not isinstance(value, CommentedSeq)
                        or cast(_SequenceStyle, value).anchor.value is not None
                        or shared
                        or not valid
                    ):
                        changed = True
                    recording["isrcs"] = normalized
    return changed
