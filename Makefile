.PHONY: help install dev test lint fmt run desktop build clean

help:
	@echo "make install  安装依赖（含 desktop 与 dev）"
	@echo "make dev      启动开发服务器（热重载）"
	@echo "make run      启动本地服务"
	@echo "make desktop  启动桌面客户端"
	@echo "make test     运行测试"
	@echo "make lint     代码检查"
	@echo "make fmt      格式化"

install:
	uv sync --extra desktop --extra dev

dev:
	uv run uvicorn kestrel.app:app --reload --host 127.0.0.1 --port 8000 --app-dir src

run:
	uv run kestrel serve

desktop:
	uv run kestrel desktop

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

fmt:
	uv run ruff format src tests

clean:
	rm -rf .venv build dist *.egg-info .pytest_cache .ruff_cache
