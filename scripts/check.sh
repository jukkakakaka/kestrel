#!/bin/sh
# 本地一键检查：lint + 语言包校验 + 全量测试。CI 跑的是同样命令。
set -e
cd "$(dirname "$0")/.."
uv run ruff check src tests scripts
uv run python scripts/check_locales.py
uv run python -m pytest -q
