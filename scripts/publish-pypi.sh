#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m pip install --upgrade ".[test]" build twine
python3 -m pytest
rm -rf dist
python3 -m build
python3 -m twine check dist/*
python3 -m twine upload --repository pypi dist/*
