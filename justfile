run:
    uv run python manage.py run

[positional-arguments]
@test *args='':
    uv run pytest "$@"
