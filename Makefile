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

e2e:
	echo "TODO"
