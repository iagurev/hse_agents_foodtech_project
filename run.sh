#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

command -v uv >/dev/null || { echo "Установите uv: https://docs.astral.sh/uv/getting-started/installation/" >&2; exit 1; }
command -v pnpm >/dev/null || { echo "Установите pnpm: https://pnpm.io/installation" >&2; exit 1; }
for database in data/recipes.sqlite data/products.sqlite data/sources.sqlite; do
  if [[ ! -f "$database" ]] || [[ "$(head -c 7 "$database")" == "version" ]]; then
    command -v git-lfs >/dev/null || { echo "Для загрузки каталогов установите git-lfs." >&2; exit 1; }
    git lfs install --local
    git lfs pull
    break
  fi
done

uv sync --locked
uv run python -m app.catalog_check
if [[ ! -d frontend/node_modules ]]; then
  pnpm --dir frontend install --frozen-lockfile
fi
pnpm --dir frontend build
exec uv run meal-app
