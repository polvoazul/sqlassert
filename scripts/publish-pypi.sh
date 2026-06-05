#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m pip install --upgrade ".[test]" build twine
python3 -m pytest

if [[ -n "$(git status --porcelain)" ]]; then
    echo "Refusing to publish a dirty worktree. Commit or stash changes first." >&2
    exit 1
fi

if git describe --exact-match --tags --match 'v[0-9]*.[0-9]*.[0-9]*' HEAD >/dev/null 2>&1; then
    VERSION_TAG="$(git describe --exact-match --tags --match 'v[0-9]*.[0-9]*.[0-9]*' HEAD)"
    echo "Publishing $VERSION_TAG"
else
    LATEST_TAG="$(git tag --list 'v[0-9]*.[0-9]*.[0-9]*' --sort=-v:refname | head -n 1)"
    if [[ -z "$LATEST_TAG" ]]; then
        VERSION_TAG="v0.1.1"
    else
        VERSION="${LATEST_TAG#v}"
        IFS=. read -r MAJOR MINOR PATCH <<< "$VERSION"
        VERSION_TAG="v$MAJOR.$MINOR.$((PATCH + 1))"
    fi

    git tag "$VERSION_TAG"
    echo "Tagged $VERSION_TAG"
fi

rm -rf dist
python3 -m build
python3 -m twine check dist/*
python3 -m twine upload --repository pypi dist/*
