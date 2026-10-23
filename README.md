<img src="https://raw.githubusercontent.com/moongas-org/moongas-mediatunes-web-vue/refs/heads/main/client/public/moongas.svg" width="128" height="128">

# moongas-mediascripts-python  

> 🚧 **Status: Work in Progress (WIP)**  
> This project is currently under active development. Features, APIs, and documentation are subject to change.

---

## Overview

**Python scripts for working with Moongas media collections.**

### A component of the `moongas` ecosystem of media library tools

- [moongas-collection-demo](https://github.com/moongas-org/moongas-collection-demo) [![CI](https://github.com/moongas-org/moongas-collection-demo/actions/workflows/ci.yml/badge.svg)](https://github.com/moongas-org/moongas-collection-demo/actions/workflows/ci.yml) - Example Moongas media collection (metadata only)
- [moongas-mediatunes-web-vue](https://github.com/moongas-org/moongas-mediatunes-web-vue) [![CI](https://github.com/moongas-org/moongas-mediatunes-web-vue/actions/workflows/ci.yml/badge.svg)](https://github.com/moongas-org/moongas-mediatunes-web-vue/actions/workflows/ci.yml) - A Deno-tooled TypeScript/Vue SPA for Moongas hybrid media collections, pairing with the separate moongas-mediatunes-svc-python-blacksheep backend to seemlessly blend offline and streaming playback
- [moongas-mediatunes-svc-python-blacksheep](https://github.com/moongas-org/moongas-mediatunes-svc-python-blacksheep) [![CI](https://github.com/moongas-org/moongas-mediatunes-svc-python-blacksheep/actions/workflows/ci.yml/badge.svg)](https://github.com/moongas-org/moongas-mediatunes-svc-python-blacksheep/actions/workflows/ci.yml) - Python+BlackSheep implementation of API service for Moongas hybrid media collections—backend for Moongas mediatunes web application (moongas-mediatunes-web-vue)
- [moongas-mediatunes-svc-java-javalin](https://github.com/moongas-org/moongas-mediatunes-svc-java-javalin) [![CI](https://github.com/moongas-org/moongas-mediatunes-svc-python-blacksheep/actions/workflows/ci.yml/badge.svg)](https://github.com/moongas-org/moongas-mediatunes-svc-java-javalin/actions/workflows/ci.yml) - Java+Javalin implementation of API service for Moongas hybrid media collections—backend for Moongas mediatunes web application (moongas-mediatunes-web-vue)
- [moongas-mediascan-go](https://github.com/moongas-org/moongas-mediascan-go) [![CI](https://github.com/moongas-org/moongas-mediascan-go/actions/workflows/ci.yml/badge.svg)](https://github.com/moongas-org/moongas-mediascan-go/actions/workflows/ci.yml) - Golang module to scan media collections and Moongas Yaml metatadata, outputs Moongas database
- [moongas-mediascan-python](https://github.com/moongas-org/moongas-mediascan-python) [![CI](https://github.com/moongas-org/moongas-mediascan-python/actions/workflows/ci.yml/badge.svg)](https://github.com/moongas-org/moongas-mediascan-python/actions/workflows/ci.yml) - Python library with data classes for loading Moongas mediascan databases and Yaml metadata files
- [moongas-mediascripts-python](https://github.com/moongas-org/moongas-mediascripts-python) [![CI](https://github.com/moongas-org/moongas-mediascripts-python/actions/workflows/ci.yml/badge.svg)](https://github.com/moongas-org/moongas-mediascripts-python/actions/workflows/ci.yml) - Python scripts for working with Moongas media collections.
- [moongas-mediatest-python-pytest](https://github.com/moongas-org/moongas-mediatest-python-pytest) [![CI](https://github.com/moongas-org/moongas-mediatest-python-pytest/actions/workflows/ci.yml/badge.svg)](https://github.com/moongas-org/moongas-mediatest-python-pytest/actions/workflows/ci.yml) - Python tool for enforcing media collection rules (implemented with `pytest`)

## Live Demos
- [Live Demo (hosted on moongas.org)](https://moongas.org/mediaserver)
- [Live Demo (hosted on moongas.org)](https://moongas.org/mediaserver)

```mermaid
graph TD;
    A[Start] --- B(Choose Frontend and Backend);
    B --- C{Choose Backend};
    C ---|Python| D[mediatunes-svc-python-blacksheep];
    C ---|Java| E[mediatunes-svc-java-javalin];
    B --- F{Choose Frontend};
    F ---|Deno+Vue| G[mediatunes-web-vue];
    F ---|TBD| H[tbd];
    D ---|has dependency| I[mediascan-python];
    I ---|loads| J[mediascan.db];
    J ---|generates| K[mediascan-go];
    E ---|loads| J[mediascan.db];
    J ---|validates| L[mediatest-python-pytest];
    J ---|reads readonly| M[mediascripts-python];
```

# Quick Start

```bash
pip install "git+https://github.com/moongas-org/moongas-mediascripts-python.git[stats]"
```

### (Developer) Clone GitHub repo and install (editable)

```bash
git clone git@github.com:moongas-org/moongas-mediascan-python.git
cd moongas-mediascan-python
python -m pip install -e ".[dev,llm,stats]"
```

### Optional dependency groups

- `[dev]` - development dependencies (includes `pytest` and `ruff`)
- `[stats]` - statistics script dependencies (includes `matplotlib` and `numpy`)

# Usage

The `mediascripts` package installs a console script named `mediascripts` so it can be executed either with `python -m mediascripts` or simply with `mediascripts`.

```bash
$ mediascripts --h
usage: mediascripts [-h] [--list] [--create-script-symlinks]

Utility for media* console script entry points.

options:
  -h, --help            show this help message and exit
  --list                list all media* console script entry points, grouped by scripts subdirectory
  --create-script-symlinks
                        create hyphenated symlinks in the current directory to every console entry script and every shell script in
                        scripts/shell
```

The `--list` option may be used to list all console scripts and shell scripts installed by any `media*` packages.
```bash
$ mediascripts --list
artist:
  artist-countries-to-mapgraph-json
  artist-csv-to-artist-yaml
  artist-yaml-auto-populate
  artist-yaml-reformat-with-files-yaml
  artist-yaml-test-with-files-yaml
  list-missing-region-names
convert:
  convert-covers
  convert-videos
  make-covers-video
copy:
  copy-medialib
dev:
  generate-dataclasses
lyrics:
  lyrics-text-auto-populate
media_files_yaml:
  list-artists-from-files-yaml
  play-rand-file-from-files-yaml
mediascan_db:
  list-albums
  list-artists
  list-mediafiles
rename:
  rename-album-files
stats:
  genre-clusters
  mediastats
track:
  compare-track-yaml-with-media-file
```

## Compare Track YAML with MP3 Tags

With `MOONGAS_COLLECTION_ROOTDIR` set, run:

```bash
compare-track-yaml-with-media-file
compare-track-yaml-with-media-file --quiet
compare-track-yaml-with-media-file --dir "/path/to/music" --quiet
```

`-d` / `--dir` recursively scans the specified directory and all subdirectories,
without requiring `MOONGAS_COLLECTION_ROOTDIR` or a collection folder structure.
It takes precedence over the environment variable.

The script walks album directories under `$MOONGAS_COLLECTION_ROOTDIR/data`,
following library symlinks, and compares each MP3 with its sibling `<track>.yml`.
It uses Mutagen to compare the six core `trackData` fields: `title`, `artist`,
`albumartist`, `album`, `genre`, and `year` (against the ID3 date's year).
Leading/trailing whitespace is ignored; additional ID3 values are reported as
differences. Extra tags and streaming identifiers are not compared.

Tracks are skipped before reading YAML or media tags when their artist directory
contains any newline-separated keyword in `COPY_MEDIALIB_EXCLUDE_KEYWORDS`.
The YAML `artist` is also checked when the directory is not excluded. Matching is
case-sensitive, as in the copy scripts; blank lines are ignored. For example:

```bash
export COPY_MEDIALIB_EXCLUDE_KEYWORDS=$'Excluded Artist\nAnother Artist'
```

`-q` / `--quiet` suppresses matching tracks, but still reports differences,
missing YAML or tags, malformed YAML, and unreadable media files.
Files are never modified. Exit status is 0 if all tracks match, or 1 for any
difference or error. It also runs as
`python -m mediascripts.track.track_yaml.compare_track_yaml_with_media_file`.

## List Mediascan Database Tables

Set `MOONGAS_COLLECTION_ROOTDIR` to the active Moongas collection, then run:

```bash
list-artists
list-albums
list-mediafiles
```

These scripts use the `mediascan` library to open
`$MOONGAS_COLLECTION_ROOTDIR/mediascan.db` and print every row and column of the
`artist`, `album`, and `mediafile` tables, respectively, without modifying the
database. All headers and values are left-aligned, with no row, column, or value
truncation. The collection variable and an existing database file are required.

The modules can also be run with
`python -m mediascripts.mediascan_db.list_artists`,
`python -m mediascripts.mediascan_db.list_albums`, or
`python -m mediascripts.mediascan_db.list_mediafiles`.

## `scan-to-album-yaml` - Create Album YAML Files

With `MOONGAS_COLLECTION_ROOTDIR` set and `moongas-mediascan-go` available beneath
that directory, run:

```bash
mediascripts --create-script-symlinks
./scan-to-album-yaml
```

This uses the collection's `mediascan-config.yml` to create missing `album.yml`
files. Existing album YAML files are never overwritten. Additional arguments are
passed through to the Go scanner.

## `show-env` - Show Moongas Environment Variables
```bash
user@host:~/Git/moongas-org/moongas/moongas-collection-local$ ./show-env 
------------------------------------------------------------------------------
                             Moongas Environment                              
------------------------------------------------------------------------------
  MOONGAS_COLLECTION_ROOTDIR:
    "/home/user/Git/moongas-org/moongas/moongas-collection-local"
  MOONGAS_REMOTE_SERVER_IP:
    "159.89.93.193"
------------------------------------------------------------------------------

```
