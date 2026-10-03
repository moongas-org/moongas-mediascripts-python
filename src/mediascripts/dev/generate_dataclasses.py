import argparse
import json
import sys

import yaml
from dataclass_wizard.v0.wizard_cli.schema import PyCodeGenerator


def load_yaml_file(yaml_fname: str):
    data = None
    with open(yaml_fname, "r") as stream:
        try:
            data = yaml.safe_load(stream)
        except yaml.YAMLError as exc:
            print(exc)
            sys.exit(1)
    return data


def main():
    parser = argparse.ArgumentParser(
        description="Generates Python dataclasses from a YAML file schema."
    )
    parser.add_argument(
        "yaml_file",
        nargs="?",
        default="files.yml",
        help="Path to the YAML file (default: %(default)s)",
    )
    args = parser.parse_args()
    data = load_yaml_file(args.yaml_file)
    print(PyCodeGenerator(json.dumps(data), experimental=True).py_code)


if __name__ == "__main__":
    main()
