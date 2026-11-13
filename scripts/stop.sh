#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
docker compose --profile gitlab stop
printf '社区版服务已停止，账号、API 配置和作品保留。\n'
