#!/bin/sh
# 本地一键检查：lint + 测试。CI 跑的是同样两条命令。
set -e
cd "$(dirname "$0")/.."
uv run ruff check src tests
uv run python -m pytest -q
