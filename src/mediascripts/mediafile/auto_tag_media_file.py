"""Set embedded metadata from explicit values, track YAML, and collection paths."""

import argparse
import base64
import logging
import re
import struct
from collections import Counter
from collections.abc import Mapping, MutableMapping
from io import BytesIO
from pathlib import Path
from typing import Protocol, TextIO, cast

import mutagen
from mediascan.types.album.album_path import AlbumPathBuilder
from mediascan.types.genre.genres import Genre
from mutagen.asf import ASF
from mutagen.asf._attrs import ASFByteArrayAttribute, ASFUnicodeAttribute
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC, COMM, ID3, ID3NoHeaderError, PictureType, TALB, TCOM, TCON, TCOP,
    TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TextFrame,
)
from mutagen.mp4 import MP4, MP4Cover, MP4Tags
from mutagen.ogg import OggFileType
from mutagen.wave import WAVE
from PIL import Image
from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from mediascripts.lyrics.lyrics_common import MEDIA_EXTENSIONS

logger = logging.getLogger(__name__)

FIELDS = ("artist", "albumartist", "album", "title", "genre", "year")
OPTIONAL_FIELDS = ("comment", "composer", "copyright")
POSITION_FIELDS = {"tracknumber": "track", "discnumber": "disc"}
ID3_FRAMES: dict[str, type[TextFrame]] = {
    "artist": TPE1, "albumartist": TPE2, "album": TALB, "title": TIT2, "genre": TCON, "year": TDRC,
    "tracknumber": TRCK, "discnumber": TPOS, "comment": COMM, "composer": TCOM, "copyright": TCOP,
}
MP4_KEYS: dict[str, str] = dict(zip(FIELDS, ("\xa9ART", "aART", "\xa9alb", "\xa9nam", "\xa9gen", "\xa9day")))
MP4_KEYS.update({"comment": "\xa9cmt", "composer": "\xa9wrt", "copyright": "cprt"})
ASF_KEYS: dict[str, str] = dict(zip(FIELDS, ("Author", "WM/AlbumArtist", "WM/AlbumTitle", "Title", "WM/Genre", "WM/Year")))
ASF_KEYS["tracknumber"] = "WM/TrackNumber"
ASF_KEYS["discnumber"] = "WM/PartOfSet"
ASF_KEYS.update({"comment": "Description", "composer": "WM/Composer", "copyright": "Copyright"})


class _YamlReader(Protocol):
    def load(self, stream: TextIO) -> object: ...


class _ID3Access(Protocol):
    def getall(self, key: str) -> list[object]: ...
    def delall(self, key: str) -> None: ...
    def add(self, frame: object) -> None: ...


class _TextFrame(Protocol):
    text: list[object]


class _PictureAccess(Protocol):
    pictures: list[Picture]

    def add_picture(self, picture: Picture) -> None: ...


class _MediaWriter(Protocol):
    def save(self, filething: Path) -> None: ...


class _TagDisplay(Protocol):
    def pprint(self) -> str: ...


class _PictureDetails(Protocol):
    type: int
    mime: str
    desc: str
    data: bytes


def print_tags(path: Path, media: ID3 | mutagen.FileType, dry_run: bool) -> None:
    tags = media if isinstance(media, ID3) else media.tags
    print(f"{path}: {'Proposed tags (dry-run; not saved)' if dry_run else 'Post-update tags'}")
    if tags is not None:
        text = cast(_TagDisplay, tags).pprint()
        if text:
            print(text)
    if isinstance(media, FLAC):
        for index, picture in enumerate(cast(_PictureAccess, media).pictures, start=1):
            details = cast(_PictureDetails, picture)
            print(
                f"PICTURE[{index}]: type={details.type}, mime={details.mime}, "
                f"description={details.desc!r}, size={len(details.data)} bytes"
            )


def unescape_name(name: str) -> str:
    """Best-effort inverse of rename_album_files' lossy period replacement."""
    return name.replace("_", ".").strip()


def read_track_values(path: Path) -> dict[str, str]:
    candidates = sorted(
        candidate for candidate in path.parent.iterdir()
        if candidate.is_file() and candidate.stem == path.stem
        and candidate.suffix.lower() in {".yml", ".yaml"}
    )
    if len(candidates) > 1:
        raise ValueError(f"Multiple track YAML files for {path}: {candidates}")
    if not candidates:
        return {}
    with candidates[0].open(encoding="utf-8") as stream:
        document = cast(_YamlReader, YAML(typ="safe")).load(stream)
    if not isinstance(document, dict):
        raise ValueError(f"Expected a YAML mapping in {candidates[0]}")
    mapping = cast(Mapping[str, object], document)
    data = mapping.get("trackData", mapping.get("track_data", {}))
    if not isinstance(data, dict):
        raise ValueError(f"Expected a trackData mapping in {candidates[0]}")
    values: dict[str, str] = {}
    metadata = cast(Mapping[str, object], data)
    for field in FIELDS:
        value = metadata.get(field)
        if value is None or value == "" or (field == "year" and value == 0):
            continue
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError(f"Invalid track YAML {field}: {value!r}")
        if str(value).strip():
            values[field] = str(value).strip()
    logger.info("[%s] Using track YAML: %s", path, candidates[0])
    return values


def resolve_values(path: Path, overrides: dict[str, str]) -> dict[str, str]:
    album_path = AlbumPathBuilder.of(path.parent)
    album_name = re.sub(r"\s*\[\d{4}\]\s*$", "", album_path.album_dirname)
    title = re.sub(r"^\d+(?:[.-]\d+)?\s*[-.]\s+|^\d+\s+", "", unescape_name(path.stem))
    values = {
        "artist": unescape_name(album_path.artist_dirname),
        "albumartist": unescape_name(album_path.artist_dirname),
        "album": unescape_name(album_name),
        "title": title.strip(),
    }
    if album_path.year:
        values["year"] = str(album_path.year)
    values.update(read_track_values(path))
    values.update(overrides)
    for field, value in values.items():
        if field not in (*OPTIONAL_FIELDS, *POSITION_FIELDS, "genre") and not value.strip():
            raise ValueError(f"{field} cannot be empty")
    if "year" in values and not re.fullmatch(r"[0-9]{4}", values["year"]):
        raise ValueError(f"Expected a four-digit year, got {values['year']!r}")
    return values


def load_media(path: Path) -> ID3 | mutagen.FileType:
    if path.suffix.lower() == ".mp3":
        try:
            return ID3(path)
        except ID3NoHeaderError:
            return ID3()
    media = mutagen.File(path)
    if not isinstance(media, (MP4, FLAC, OggFileType, WAVE, ASF)):
        raise ValueError(f"Unsupported writable media format: {path.suffix}")
    if media.tags is None:
        media.add_tags()
    return media


def text_values(media: ID3 | mutagen.FileType, field: str, value: str | None = None) -> list[str]:
    tags = media if isinstance(media, ID3) else media.tags
    if isinstance(tags, ID3):
        frame_type = ID3_FRAMES[field]
        key = frame_type.__name__
        id3 = cast(_ID3Access, tags)
        previous = [str(text) for frame in id3.getall(key) for text in cast(_TextFrame, frame).text]
        if value is not None:
            id3.delall(key)
            if value:
                if field == "comment":
                    id3.add(COMM(encoding=3, lang="eng", desc="", text=[value]))
                else:
                    id3.add(frame_type(encoding=3, text=[value]))
        return previous
    if isinstance(tags, MP4Tags):
        if field in POSITION_FIELDS:
            key = "trkn" if field == "tracknumber" else "disk"
            track_tags = cast(MutableMapping[str, list[tuple[int, int]]], tags)
            tracks = track_tags.get(key, [])
            previous = [f"{number}/{total}" if total else str(number) for number, total in tracks]
            if value is not None:
                if value:
                    number, _, total = value.partition("/")
                    track_tags[key] = [(int(number), int(total) if total else 0)]
                elif key in track_tags:
                    del track_tags[key]
            return previous
        key = MP4_KEYS[field]
    elif isinstance(media, ASF):
        key = ASF_KEYS[field]
    else:
        key = "date" if field == "year" else field
    mapping = cast(MutableMapping[str, list[object]], tags)
    previous = [str(item) for item in mapping.get(key, [])]
    total_keys: tuple[str, ...] = ()
    if field in POSITION_FIELDS and not isinstance(media, ASF):
        total_keys = ("tracktotal", "totaltracks") if field == "tracknumber" else ("disctotal", "totaldiscs")
        for total_key in total_keys:
            totals = mapping.get(total_key, [])
            if totals:
                previous = [f"{item}/{totals[0]}" if "/" not in item else item for item in previous]
                if not previous:
                    previous = [f"/{totals[0]}"]
                break
    if value is not None:
        for total_key in total_keys:
            if total_key in mapping:
                del mapping[total_key]
        if value == "":
            if key in mapping:
                del mapping[key]
        else:
            mapping[key] = [ASFUnicodeAttribute(value)] if isinstance(media, ASF) else [value]
    return previous


def has_cover(media: ID3 | mutagen.FileType) -> bool:
    tags = media if isinstance(media, ID3) else media.tags
    if isinstance(tags, ID3):
        return bool(cast(_ID3Access, tags).getall("APIC"))
    if isinstance(media, FLAC):
        return bool(cast(_PictureAccess, media).pictures)
    mapping = cast(MutableMapping[str, object], tags)
    return any(mapping.get(key) for key in ("covr", "metadata_block_picture", "coverart", "WM/Picture"))


def infer_artist_genre(path: Path) -> str | None:
    """Count each neighboring track once, preferring valid YAML over media tags."""
    valid_genres = {genre.value for genre in Genre}
    counts: Counter[str] = Counter()
    for album in sorted(path.parent.parent.iterdir()):
        if not album.is_dir():
            continue
        tracks: dict[str, list[Path]] = {}
        for candidate in sorted(album.iterdir()):
            if (
                not candidate.is_file()
                or candidate.suffix.lower() not in {*MEDIA_EXTENSIONS, ".yml", ".yaml"}
                or candidate.stem.lower() in {"artist", "artists", "album", "files"}
                or (album == path.parent and candidate.stem == path.stem)
            ):
                continue
            tracks.setdefault(candidate.stem, []).append(candidate)
        for candidates in tracks.values():
            genres: list[str] = []
            try:
                value = read_track_values(candidates[0]).get("genre")
                if value in valid_genres:
                    genres = [value]
                elif value:
                    logger.warning("[%s] Ignoring invalid neighboring YAML genre %r", candidates[0], value)
            except (OSError, UnicodeError, ValueError, TypeError, YAMLError) as error:
                logger.warning("[%s] Could not read neighboring track YAML: %s", candidates[0], error)
            if not genres:
                for candidate in candidates:
                    if candidate.suffix.lower() not in MEDIA_EXTENSIONS:
                        continue
                    try:
                        values = text_values(load_media(candidate), "genre")
                        genres = [value for value in values if value in valid_genres]
                        invalid = [value for value in values if value not in valid_genres]
                        if invalid:
                            logger.warning("[%s] Ignoring invalid neighboring media genres %r", candidate, invalid)
                    except (OSError, ValueError, TypeError, mutagen.MutagenError) as error:
                        logger.warning("[%s] Could not read neighboring media genres: %s", candidate, error)
                    if genres:
                        break
            counts.update(set(genres))
    if not counts:
        logger.info("[%s] No valid neighboring genres found for artist", path)
        return None
    selected = min(counts, key=lambda genre: (-counts[genre], genre))
    logger.info("[%s] Inferred artist genre %r from neighboring tracks: %s", path, selected, dict(sorted(counts.items())))
    return selected


def find_cover(path: Path) -> Path | None:
    candidates = [
        candidate for candidate in path.parent.iterdir()
        if candidate.is_file() and candidate.name.lower() in {"cover.jpg", "cover.webp"}
    ]
    return max(
        candidates,
        key=lambda candidate: (candidate.suffix.lower() == ".jpg", candidate.stat().st_size, candidate.name),
        default=None,
    )


def add_cover(media: ID3 | mutagen.FileType, path: Path) -> None:
    data = path.read_bytes()
    if data.startswith(b"\xff\xd8\xff"):
        mime = "image/jpeg"
    elif data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        raise ValueError(f"Cover must be a JPEG, PNG, or WebP image: {path}")
    tags = media if isinstance(media, ID3) else media.tags
    if isinstance(tags, ID3):
        cast(_ID3Access, tags).add(APIC(encoding=3, mime=mime, type=PictureType.COVER_FRONT, desc="Cover", data=data))
    elif isinstance(tags, MP4Tags):
        if mime == "image/webp":
            with Image.open(BytesIO(data)) as image, BytesIO() as output:
                image.convert("RGB").save(output, format="JPEG", quality=95)
                data = output.getvalue()
            mime = "image/jpeg"
            logger.info("Converting WebP cover to JPEG for MP4/M4A embedding: %s", path)
        image_format = MP4Cover.FORMAT_JPEG if mime == "image/jpeg" else MP4Cover.FORMAT_PNG
        tags["covr"] = [MP4Cover(data, imageformat=image_format)]
    elif isinstance(media, ASF):
        picture = struct.pack("<BI", 3, len(data)) + (mime + "\0\0").encode("utf-16-le") + data
        cast(MutableMapping[str, list[object]], tags)["WM/Picture"] = [ASFByteArrayAttribute(picture)]
    else:
        picture = Picture()
        picture.type = 3
        picture.mime = mime
        picture.desc = "Cover"
        picture.data = data
        if isinstance(media, FLAC):
            cast(_PictureAccess, media).add_picture(picture)
        else:
            mapping = cast(MutableMapping[str, list[str]], tags)
            mapping["metadata_block_picture"] = [base64.b64encode(picture.write()).decode("ascii")]


def auto_tag_media_file(
    path: Path, *, dry_run: bool = False, cover: Path | None = None, verbose: bool = False, **overrides: str,
) -> bool:
    """Return whether tags changed; dry-run validates the same edits without saving."""
    path = path.absolute()
    if not path.is_file():
        raise FileNotFoundError(f"Media file does not exist: {path}")
    if path.suffix.lower() not in MEDIA_EXTENSIONS:
        raise ValueError(f"Not a recognized media extension: {path.suffix}")
    unknown = overrides.keys() - (set(FIELDS) | set(OPTIONAL_FIELDS) | POSITION_FIELDS.keys())
    if unknown:
        raise ValueError(f"Unknown metadata fields: {sorted(unknown)}")
    values = resolve_values(path, overrides)
    for field, option in POSITION_FIELDS.items():
        if field not in values or values[field] == "":
            continue
        position = values[field]
        if not re.fullmatch(r"[0-9]+(?:/[0-9]+)?", position):
            raise ValueError(f"--{option} must be number or number/total, got {position!r}")
        number_text, _, total_text = position.partition("/")
        if not 0 < int(number_text) < 100 or (total_text and not 0 < int(total_text) <= 65535):
            raise ValueError(f"--{option} requires a position from 1 to 99 and a total from 1 to 65535")
        values[field] = str(int(number_text)) + (f"/{int(total_text)}" if total_text else "")
    prefix = re.match(r"[0-9]+", path.stem)
    if "tracknumber" not in values and prefix is not None:
        number = int(prefix.group())
        if 0 < number < 100:
            values["tracknumber"] = str(number)
        else:
            logger.warning("[%s] Ignoring filename track number outside 1-99: %s", path, prefix.group())
    media = load_media(path)
    genres = [values["genre"]] if "genre" in values else text_values(media, "genre")
    valid_genres = {genre.value for genre in Genre}
    invalid_genres = [genre for genre in genres if genre not in valid_genres]
    if invalid_genres:
        logger.warning(
            "[%s] Invalid genre values %r are not in the mediascan Genre enum; %s genre",
            path, invalid_genres, "would unset" if dry_run else "unsetting",
        )
        values["genre"] = ""
    if "genre" not in overrides and not any(genre in valid_genres for genre in genres):
        inferred_genre = infer_artist_genre(path)
        if inferred_genre is not None:
            values["genre"] = inferred_genre
    changes: list[tuple[str, object, object]] = []
    for field, value in values.items():
        previous = text_values(media, field)
        if field == "tracknumber" and field not in overrides and previous:
            _, separator, total = previous[0].partition("/")
            if separator:
                value += separator + total
        if previous != ([] if value == "" else [value]):
            text_values(media, field, value)
            changes.append((field, previous, value))
    if not has_cover(media):
        selected_cover = cover if cover is not None else find_cover(path)
        if selected_cover is not None:
            add_cover(media, selected_cover)
            changes.append(("cover", None, str(selected_cover)))
    elif cover is not None:
        logger.info("[%s] Preserving existing cover art; --cover is only used when art is missing", path)
    if changes and not dry_run:
        cast(_MediaWriter, media).save(path)
    for field, previous, value in changes:
        logger.info(
            "[%s] %s %s: %r -> %r", path, "Would set" if dry_run else "Set", field, previous, value,
        )
    if not changes:
        logger.info("[%s] No tag changes needed", path)
    final_media = media if dry_run else load_media(path)
    print(f"{path}: {'Proposed values (dry-run; not saved)' if dry_run else 'Final values'}")
    for field in (*FIELDS, *POSITION_FIELDS, *OPTIONAL_FIELDS):
        final_values = text_values(final_media, field)
        print(f"  {field}: {repr(final_values) if final_values else '(not set)'}")
    print(f"  cover: {'present' if has_cover(final_media) else '(not set)'}")
    if verbose:
        print_tags(path, final_media, dry_run)
    return bool(changes)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, required=True, help="Media file to tag.")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Log proposed edits without saving.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Print all tags after updating (proposed tags in dry-run).")
    for field in FIELDS:
        parser.add_argument(
            "--album-artist" if field == "albumartist" else f"--{field}",
            dest=field,
            metavar="GENRE" if field == "genre" else None,
            help=f"Override the {field} value from YAML or path clues.",
        )
    parser.add_argument("--cover", type=Path, help="Cover image to embed only if artwork is missing.")
    for option in POSITION_FIELDS.values():
        parser.add_argument(
            f"--{option}",
            help=f"Set {option} as number or number/total (position 1-99); no total removes it; empty clears the field.",
        )
    for field in OPTIONAL_FIELDS:
        parser.add_argument(f"--{field}", help=f"Set {field}; an empty string clears the field.")
    args = parser.parse_args(argv)
    if args.genre is not None:
        normalized_genre = args.genre.casefold().replace("-", "")
        canonical_genre = next(
            (genre.value for genre in Genre if genre.value.casefold().replace("-", "") == normalized_genre), None,
        )
        if canonical_genre is None:
            logger.error(
                "Invalid --genre value %r: must match a mediascan Genre value (case-insensitive; hyphens ignored)",
                args.genre,
            )
            return 1
        args.genre = canonical_genre
    overrides = {
        field: getattr(args, field) for field in (*FIELDS, *OPTIONAL_FIELDS)
        if getattr(args, field) is not None
    }
    for field, option in POSITION_FIELDS.items():
        if getattr(args, option) is not None:
            overrides[field] = getattr(args, option)
    try:
        auto_tag_media_file(args.file, dry_run=args.dry_run, cover=args.cover, verbose=args.verbose, **overrides)
    except (OSError, ValueError, TypeError, YAMLError, mutagen.MutagenError) as error:
        logger.error("[%s] Could not tag media file: %s", args.file, error)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())