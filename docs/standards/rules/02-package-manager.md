---
name: package_manager
description: Requires uv for Python dependencies and execution; settings loading is handled by config.py.
---

# Package Manager Standards

Use `uv` for Python dependency management, application execution, repository scripts, and Python checks. Keep the actual dependency manifest and lockfile consistent once they exist. Do not use `pip` or another Python package manager as an alternative.

Typical commands, once the corresponding project setup exists:

```bash
uv sync
uv add <package>
uv add --dev <development-package>
uv run <script-or-tool> <arguments>
```

Run the configured test runner through `uv` as well. For example, `uv run pytest` applies only if the project has adopted and configured pytest; it does not imply a runner already exists.

## Environment And Settings

Do not require a shell-level `source .env` before commands. Application settings are loaded and validated by `src/app/config.py` with `pydantic-settings`, as described in `06-configuration.md`. An optional `.env` supplements the process environment; its absence alone is not an error. Missing genuinely required settings must still produce a clear validation error.

Dependency installation, lint, type checks, and tests with controlled inputs should not require application credentials. Tests may inject configuration or set environment variables in isolated fixtures. Do not print local secret files while diagnosing settings; inspect declared fields and safe examples instead.

## Optional Frontend

No frontend stack is assumed. If a separately documented JavaScript/TypeScript frontend is introduced, use `pnpm` within its actual directory and follow its manifest and lockfile. Do not scaffold a frontend merely because this exception exists.
