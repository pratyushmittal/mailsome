run:
    uv run uvicorn app:app --reload

[positional-arguments]
@test *args='':
    uv run pytest "$@"
