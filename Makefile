VERSION ?=
PI_HOST ?=

.PHONY: install install-dev install-pi test lint format type-check check clean start-stream start-api start-api-central stop-stream stop-api stop deploy deploy-local deploy-central coverage fetch-stt-model

install:
	uv sync

install-dev:
	uv sync --all-extras --no-extra pi

install-pi:
	rm -rf .venv
	uv venv --system-site-packages
	uv sync --all-extras --no-extra docs

test:
	uv run pytest tests/ -v

coverage:
	uv run pytest tests/ -v --cov=src/nomothetic --cov-report=html --cov-report=term-missing

lint:
	uv run ruff check src/ tests/
	uv run black --check src/ tests/

format:
	uv run black src/ tests/
	uv run ruff check --fix src/ tests/

type-check:
	uv run mypy src/ tests/

check: lint type-check test

start:
	./scripts/start.sh all

start-stream:
	./scripts/start.sh stream

start-api:
	./scripts/start.sh api --mode device

start-api-central:
	./scripts/start.sh api --mode central

stop-stream:
	./scripts/stop.sh stream

stop-api:
	./scripts/stop.sh api

stop:
	./scripts/stop.sh all

deploy:
	./scripts/deploy.sh $(VERSION) $(PI_HOST)

deploy-local:
	./scripts/deploy.sh --local --mode device $(PI_HOST)

deploy-central:
	./scripts/deploy.sh --local --mode central $(PI_HOST)

fetch-stt-model:
	./scripts/fetch_stt_model.sh

clean:
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -type f -name "*.pyc" -delete
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf build/ dist/ .coverage htmlcov/ .pytest_cache/ .mypy_cache/

help:
	@echo "Available targets:"
	@echo "  install      - Install the package"
	@echo "  install-dev  - Install package and development dependencies"
	@echo "  install-pi   - Install package on Raspberry Pi"
	@echo "  test         - Run tests"
	@echo "  coverage     - Run tests with HTML and terminal coverage report"
	@echo "  lint         - Check code style and formatting"
	@echo "  format       - Format code with black and ruff"
	@echo "  type-check   - Run type checking with mypy"
	@echo "  check        - Run lint, type-check, and tests (release checks)"
	@echo "  deploy       - Deploy release to Raspberry Pi over SSH (VERSION=v0.x.y PI_HOST=user@host)"
	@echo "  deploy-local     - Sync and deploy device mode to Pi (PI_HOST=user@host)"
	@echo "  deploy-central   - Sync and deploy central mode to server (PI_HOST=user@host)"
	@echo "  fetch-stt-model  - Download the Vosk voice-transcription model (run on the Pi)"
	@echo "  clean        - Remove generated files and caches"
	@echo "  start        - Start both the stream and API servers in the background"
	@echo "  start-stream - Start the MJPEG stream server in the background"
	@echo "  start-api        - Start the REST API server (device mode)"
	@echo "  start-api-central - Start the REST API server (central mode)"
	@echo "  stop-stream  - Stop the MJPEG stream server"
	@echo "  stop-api     - Stop the REST API server"
	@echo "  stop         - Stop all background servers"
	@echo "  help         - Show this help"
