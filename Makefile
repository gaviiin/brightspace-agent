.PHONY: install install-media backend frontend ext test e2e e2e-ui

install:
	cd backend && uv sync
	pnpm install

# Opt-in: the lecture-recording deps (yt-dlp, parakeet-mlx, static-ffmpeg).
# Everything else works without them. NOTE: any later bare `uv sync` --
# including the one `make test` implies via `uv run` -- drops this group
# again; rerun this target after. See the README's "Lecture recordings".
install-media:
	cd backend && uv sync --group media

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

e2e-ui:
	bash scripts/e2e-ui.sh
