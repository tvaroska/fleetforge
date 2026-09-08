default:
    @just --list

# Start the dev Postgres (host port 5433) and wait for it to accept connections.
db-up:
    docker compose up -d postgres
    @until docker compose exec -T postgres pg_isready -U fleetforge >/dev/null 2>&1; do sleep 1; done

db-down:
    docker compose down

migrate:
    uv run alembic upgrade head

lint:
    uv run ruff check src/ tests/ alembic/
    uv run ruff format --check src/ tests/ alembic/

typecheck:
    uv run mypy src/

# The T1 gate: lint + types + the full suite against a real Postgres.
test: lint typecheck
    uv run pytest tests/ -x -v
