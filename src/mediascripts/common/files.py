import random
from collections.abc import Iterable
from pathlib import Path


def shuffled_files(paths: Iterable[Path]) -> list[Path]:
    files = list(paths)
    random.shuffle(files)
    return files
