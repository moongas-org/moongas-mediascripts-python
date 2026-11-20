"""Shared configuration for ruamel.yaml handlers."""

from ruamel.yaml import YAML


def create_yaml(typ: str = "rt") -> YAML:
    """Create a YAML handler with the project's standard output width."""
    yaml = YAML(typ=typ)
    yaml.width = 4096
    return yaml
