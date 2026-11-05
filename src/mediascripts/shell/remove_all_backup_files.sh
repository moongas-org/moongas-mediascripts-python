#!/bin/bash
# Script to remove all backup yaml files (files ending with .yml.bak) in the current directory and its subdirectories.
# Excludes virtual environment directories (.venv and .venv.314) from the search.
# Follows symlinks during the search (because I need it to), hence the unfortunate complexity
find -H . -not -path '*/.venv' -not -path '*/.venv/*' \
        -not -path '*/.venv.314' -not -path '*/.venv.314/*' \
        -name "*.yml.bak" -type f -delete
