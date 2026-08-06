.PHONY: install backend frontend ext test e2e

install:
	cd backend && uv sync
	pnpm install

backend:
	cd backend && uv run brightspace-agent

frontend:
	cd frontend && pnpm dev

ext:
	cd extension && pnpm build

test:
	cd backend && uv run pytest
	cd extension && pnpm test
	cd frontend && pnpm test

e2e:
	cd backend && uv run python ../scripts/e2e.py
