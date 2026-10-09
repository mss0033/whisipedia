# Contributing

- Read `docs/PLAN.md` and the ADRs first.
- Conventional Commits (`feat:`, `fix:`, `docs:`, `chore:`, …). Commits are signed.
- Install the hooks: `uvx pre-commit install --hook-type pre-commit --hook-type commit-msg`.
- Never commit corpus data. Test fixtures must be small (< 1 MB per file) and license-clean.
- Changes to public APIs or storage formats need an ADR.
