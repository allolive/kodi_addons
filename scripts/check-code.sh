#!/bin/bash
# Static checks for our code: ruff (lint), mypy (types, per addon) and shellcheck.
# Settings are in pyproject.toml. Needs ruff, mypy and shellcheck on PATH; exits 1 on any finding.
set -uo pipefail
# shellcheck source=scripts/lib.sh
. "$(dirname "$0")/lib.sh"
cd "$ROOT" || exit 1

fail=0
step() { echo "== $*"; "$@" || fail=1; }

step ruff check addons tests scripts

# mypy one addon at a time: each has its own entry scripts and resources/lib packages
for addon in addons/*/; do
  scripts=("$addon"*.py)
  [ -e "${scripts[0]}" ] || scripts=()
  packages=()
  for pkg in "$addon"resources/lib/*/; do      # regular or namespace packages
    pymods=("$pkg"*.py)
    [ -e "${pymods[0]}" ] && packages+=(-p "$(basename "$pkg")")
  done
  [ ${#scripts[@]} -gt 0 ] || [ ${#packages[@]} -gt 0 ] || continue
  # packages and loose scripts cannot share one mypy run
  if [ ${#packages[@]} -gt 0 ]; then
    step env MYPYPATH="$addon/resources/lib" mypy "${packages[@]}"
  fi
  if [ ${#scripts[@]} -gt 0 ]; then
    step env MYPYPATH="$addon/resources/lib" mypy "${scripts[@]}"
  fi
done

step shellcheck -x build.sh scripts/*.sh

exit $fail
