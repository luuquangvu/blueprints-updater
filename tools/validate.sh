#!/usr/bin/env bash

set -euo pipefail

echo -e "\n==== Step 1: Ruff Format ====\n"
uv run ruff format

echo -e "\n==== Step 2: Ruff Check ====\n"
uv run ruff check --fix

echo -e "\n==== Step 3: Ty Check ====\n"
uv run ty check

echo -e "\n==== Step 4: Pyright ====\n"
uv run pyright

echo -e "\n==== Step 5: Interrogate ====\n"
uv run interrogate

echo -e "\n==== Step 6: Pytest ====\n"
uv run pytest

echo -e "\n==== Step 7: Prettier ====\n"
npx prettier --write .

echo -e "\n=== All Validation Steps Passed Successfully! ===\n"
