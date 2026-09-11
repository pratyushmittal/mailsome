run:
    # OAuth callback query strings contain authorization codes; keep them out of logs.
    uv run uvicorn app:app --reload --port 8002 --no-access-log

[positional-arguments]
@test *args='':
    uv run pytest "$@"
