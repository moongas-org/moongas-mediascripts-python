from pathlib import Path

import pytest

from mediascripts.common import files


@pytest.mark.parametrize("names", [[], ["one.mp3"], ["one.mp3", "two.mp3", "one.mp3"]])
def test_shuffled_files_preserves_input_and_all_entries(
    monkeypatch: pytest.MonkeyPatch, names: list[str],
) -> None:
    paths = [Path(name) for name in names]
    original = paths.copy()

    def reverse_order(items: list[Path]) -> None:
        items.reverse()

    monkeypatch.setattr(files.random, "shuffle", reverse_order)
    assert files.shuffled_files(iter(paths)) == list(reversed(original))
    assert paths == original
