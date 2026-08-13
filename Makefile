.PHONY: install lock format lint type test audit build check deploy deploy-down deploy-logs

install:
	python -m pip install --require-hashes -r requirements-dev.lock
	python -m pip install --no-deps -e .

lock:
	python -m piptools compile --allow-unsafe --strip-extras --generate-hashes --output-file requirements.lock pyproject.toml
	python -m piptools compile --allow-unsafe --strip-extras --extra dev --generate-hashes --output-file requirements-dev.lock pyproject.toml

format:
	python -m ruff check --fix .
	python -m ruff format .

lint:
	python -m ruff check .
	python -m ruff format --check .

type:
	python -m mypy

test:
	python -m pytest --cov=evoagent --cov-report=term-missing --cov-report=xml

audit:
	python -m pip_audit -r requirements.lock --require-hashes --disable-pip

build:
	python -m build

check: lint type test audit build

deploy:
	./scripts/deploy.sh up

deploy-down:
	./scripts/deploy.sh down

deploy-logs:
	./scripts/deploy.sh logs

