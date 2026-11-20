import pytest

from mediascripts.common.yaml import create_yaml


@pytest.mark.parametrize("typ", ["rt", "safe", "base"])
def test_create_yaml_sets_standard_width(typ: str) -> None:
    assert create_yaml(typ=typ).width == 4096
