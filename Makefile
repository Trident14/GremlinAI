.PHONY: dev demo test install demo-app smoke

install:
	python3.12 -m venv .venv
	.venv/bin/pip install --quiet -e ".[dev]"
	.venv/bin/playwright install chromium

dev:
	@test -f .env || cp .env.example .env
	.venv/bin/uvicorn gremlinai.main:app --reload --port 8000

demo:
	@test -f .env || (echo "❌ Create a .env file first (cp .env.example .env)" && exit 1)
	@. .env 2>/dev/null; test -n "$$META_API_KEY" || \
		(echo "❌ META_API_KEY not set in .env" && exit 1)
	@. .env 2>/dev/null; test -n "$$META_API_BASE" || \
		(echo "❌ META_API_BASE not set in .env" && exit 1)
	LLM_PROVIDER=meta .venv/bin/uvicorn gremlinai.main:app --reload --port 8000

demo-app:
	@test -f .env || cp .env.example .env
	.venv/bin/flask --app tests_and_extras/demo_app/app.py run --port 5001

test:
	.venv/bin/pytest tests_and_extras/tests/ -v

smoke:
	@test -f .env || cp .env.example .env
	.venv/bin/python tests_and_extras/scripts/provider_smoke.py
