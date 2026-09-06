# Mailsome

- Stack: Starlette, SQLite, Alpine.js, and Gmail APIs/client libraries.
- Preserve Gmail's own search syntax; do not invent a separate search language.
- Use `uv` with `pyproject.toml` and `uv.lock`; do not edit the lockfile by hand.
- Use pytest-bdd for user-provided scenarios. Keep features in `tests/features/`
  and bindings in `tests/test_*.py`.
