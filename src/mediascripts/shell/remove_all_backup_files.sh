#!/bin/bash
# Script to remove all backup yaml files (files ending with .yml.bak) in the current directory and its subdirectories.
find . -name "*.yml.bak" -type f -delete
