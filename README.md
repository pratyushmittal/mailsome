# Mailsome

A personal Gmail client to manage a busy inbox without missing what matters.

## Objectives

- **Custom filter tabs:** Important emails, humans, favorite newsletters, paper
  trail, OTPs, promotions, and everything else. Add tabs and adjust filters as needed.
- **Gmail search:** Use Gmail's own search syntax through its APIs and client libraries.
- **Sender context:** Open an email in a right-hand sidebar with the sender's
  previous emails and conversations, inspired by Help Scout.

## Stack

Starlette for the backend, SQLite for local email storage, and Alpine.js for the
frontend. Gmail APIs and client libraries provide email access and search.

## Development

Install Python 3.13, `uv`, and `just`, then run:

```sh
uv sync --locked
uv run pre-commit install
just run         # Start the development server
just test        # Run pytest; accepts arguments, e.g. just test -k search
```

Dependencies are managed in `pyproject.toml` and `uv.lock`. Run `uv lock` when
editing dependencies. Run checks with `uv run pre-commit run --all-files`.

Add user-provided BDD scenarios to `tests/features/*.feature` and their
pytest-bdd bindings and steps to `tests/test_*.py`. Shared fixtures belong in
`tests/conftest.py` when needed.

The Starlette entry point has no routes yet; email features are not implemented.
No scenarios have been supplied yet, so pytest currently exits with “no tests ran”.
