import argparse
import logging
import os
from typing import Set, Tuple, cast

import numpy as np
import pandas as pd
from matplotlib import pyplot as plt
from matplotlib.axes import Axes

from mediascan.media_file_data import MediaFileData
from mediascan.media_files_yaml_file_loader import load_media_files_yaml_file

"""
mediastats.py
Reads yaml file output by mediascan and calculates statistics
"""

log_level = logging.INFO
log_format = "[%(asctime)s] [%(levelname)s] %(name)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=log_format,
    handlers=[
        logging.FileHandler("mediastats.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)


def get_year_counts(files: list[MediaFileData]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for f in files:
        year = int(f.year)
        if year in counts:
            counts[year] += 1
        else:
            counts[year] = 1
    return counts


def get_genre_counts(
    files: list[MediaFileData], min_val: int, reverse_sort: bool = False
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in files:
        genre = f.genre
        if genre in counts:
            counts[genre] += 1
        else:
            print(f"adding new genre '{genre}'")
            counts[genre] = 1
    counts_above_min_val = {k: v for (k, v) in counts.items() if v >= min_val}
    return dict(
        sorted(
            counts_above_min_val.items(), key=lambda item: item[1], reverse=reverse_sort
        )
    )


def get_file_sizes(files: list[MediaFileData]) -> list[int]:
    return [int(f.size) for f in files]


def get_file_durations(files: list[MediaFileData]) -> list[int]:
    return [int(f.duration) for f in files]


def get_years(files: list[MediaFileData]):
    return [int(f.year) for f in files]


def plt_year_counts(files: list[MediaFileData]):
    counts: dict[int, int] = get_year_counts(files)
    df = pd.DataFrame({"year": counts.keys(), "count": counts.values()})
    df.plot.bar(x="year", y="count", rot=90)  # type: ignore


def plt_genre_counts(files: list[MediaFileData], min_val: int):
    counts = get_genre_counts(files, min_val)
    df = pd.DataFrame({"genre": counts.keys(), "count": counts.values()})
    df.plot.barh(x="genre", y="count", rot=0)  # type: ignore
    plt.yticks(fontsize=9)  # type: ignore


def plt_file_sizes(files: list[MediaFileData]):
    sizes = get_file_sizes(files)
    plt.hist(sizes, bins=1000)  # type: ignore
    MEGABYTE = 10**6
    # MEBIBYTE = 2**20
    conversion_unit: int = MEGABYTE
    axes: Axes = plt.gca()
    current_values = cast(list[float], axes.get_xticks().tolist())
    labels: list[str] = ["{:.2f}".format(x / conversion_unit) for x in current_values]
    axes.set_xticklabels(labels)  # type: ignore


def plt_file_durations(files: list[MediaFileData]):
    durations = get_file_durations(files)
    plt.hist(durations, bins=range(800), color="b", edgecolor="red")  # type: ignore
    plt.xticks(rotation="vertical")  # type: ignore
    plt.xlim(0, 800)  # type: ignore
    plt.ylim(0, 100)  # type: ignore
    xtick_min = min(durations)
    # xtick_max = max(sizes)+1
    xtick_max = 800
    tick_positions: list[float] = [
        float(value) for value in np.arange(xtick_min, xtick_max, 10.0)
    ]
    axes: Axes = plt.gca()
    axes.set_xticks(tick_positions)  # type: ignore[reportUnknownMemberType]


def plt_year_vs_duration(files: list[MediaFileData]):
    years = get_years(files)
    durations = get_file_durations(files)
    plt.scatter(x=years, y=durations)  # type: ignore
    plt.xlim(1950, 2022)  # type: ignore
    plt.ylim(0, 3000)  # type: ignore


def print_genres(files: list[MediaFileData]):
    genres: Set[str] = set()  # type: ignore
    for f in files:
        genres.add(f.genre)
    print(sorted(genres))


def print_genre_counts(files: list[MediaFileData]):
    counts = get_genre_counts(files, 1, reverse_sort=True)
    print(counts)


def get_album_paths(files: list[MediaFileData]) -> Set[Tuple[str, str]]:
    """returns set of tuples of (album,path)"""
    albums: Set[Tuple[str, str]] = set()
    for f in files:
        albums.add((f.album, os.path.dirname(f.path)))
    return albums


def print_covers_by_size(files: list[MediaFileData]):
    """Use this to find low-resolution album cover files that need to be updated to high-res"""
    album_paths: Set[Tuple[str, str]] = get_album_paths(files)
    cover_paths: list[Tuple[str, int]] = []
    for _, path in album_paths:
        cover_path = os.path.join(path, "cover.jpg")
        cover_size = os.path.getsize(cover_path)
        cover_paths.append((cover_path, cover_size))
    cover_paths = sorted(cover_paths, key=lambda item: item[1], reverse=True)
    for cover_path, size in cover_paths:
        print(f"{cover_path} {size}")


def main():
    parser = argparse.ArgumentParser(
        description="Calculates statistics from a media files YAML file."
    )
    parser.add_argument("files_yaml_file", help="Path to the files YAML file")
    args = parser.parse_args()
    files_yaml_file = load_media_files_yaml_file(args.files_yaml_file)
    files = files_yaml_file.files
    # print_covers_by_size(files)
    # print_genre_counts(files)
    # print_genres(files)
    # plt_year_counts(files)
    plt_genre_counts(files, 20)
    # plt_file_sizes(files)
    # plt_file_durations(files)
    # plt_year_vs_duration(files)
    plt.show()  # type: ignore


if __name__ == "__main__":
    main()
