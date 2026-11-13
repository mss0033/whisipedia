# Contributing

- Read `docs/PLAN.md` and the ADRs first.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, …). Commits are signed.
- Run `tools/gate` before merging (ruff, pyright, every test directory under a memory cap; it refuses to
  run beside the index build and stamps the commit it passed). CI repeats it and runs gitleaks and the
  Navigator's Playwright journeys. The pre-commit hooks are optional:
  `uvx pre-commit install --hook-type pre-commit --hook-type commit-msg`.
- Never commit corpus data. Test fixtures must be small (< 1 MB per file) and license-clean.
- Changes to public APIs or storage formats need an ADR.
- A script or test that calls the running API marks itself (`X-Whisipedia-Probe: <name>`, or
  `whisipedia-probe` in its User-Agent, or `WSP_API_PROBE=1` for `wsp --url`): unmarked, it counts as
  the owner reading and freezes the background jobs (`docs/components/api.md`).
