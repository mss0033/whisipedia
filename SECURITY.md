# Security Policy

This is a private repository. Report suspected vulnerabilities or leaked secrets to the repository owner directly. Do not open an issue for them.

Hygiene rules:
- No credentials, tokens, host names, IP addresses or personal paths are committed. Machine-specific values go in `config/local.toml` (gitignored) or environment variables.
- `gitleaks` runs in pre-commit and CI.
- If a secret is ever committed, rotate it first, then rewrite history.
