import argparse


def parse_boolean(value: str) -> bool:
    """Parse a command-line boolean value."""
    normalized_value = value.lower()
    if normalized_value == "true":
        return True
    if normalized_value == "false":
        return False
    raise argparse.ArgumentTypeError("expected true or false")
