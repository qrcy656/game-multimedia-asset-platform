#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--with-gitlab" && "$1" != "--without-gitlab" ) ]]; then
  printf 'Usage: bash scripts/start.sh [--with-gitlab|--without-gitlab]\n' >&2
  exit 2
fi
project_directory=$(pwd -P)
if [[ "$project_directory" == *','* || "$project_directory" == *$'\n'* ]]; then
  printf 'Project directory cannot contain a comma or newline.\n' >&2
  exit 1
fi
docker run --rm --network none --read-only --user 0:0 \
  --cap-drop ALL --cap-add CHOWN --cap-add DAC_OVERRIDE \
  --security-opt no-new-privileges \
  --mount "type=bind,src=${project_directory},dst=/workspace" \
  python:3.12-slim-bookworm python /workspace/scripts/initialize.py "$(id -u)" "$(id -g)"
if [[ "${1:-}" == "--without-gitlab" ]]; then
  COMPOSE_PROFILES= docker compose up -d --build app animation-worker effects postgres redis
elif [[ "${1:-}" == "--with-gitlab" ]]; then
  docker compose --profile gitlab up -d --build
else
  docker compose up -d --build
fi
access_url=$(sed -n 's/^APP_ORIGIN=//p' .env | tail -n 1)
access_url=${APP_ORIGIN:-${access_url%$'\r'}}
printf 'Fantasy Community is ready at %s\n' "${access_url:-http://localhost:18082}"
printf 'Bundled GitLab initializes its OAuth connection automatically; first startup can take several minutes.\n'
