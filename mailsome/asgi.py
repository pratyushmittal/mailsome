"""One local web process owns the periodic Gmail and AI workers."""

import asyncio

from django.core.asgi import get_asgi_application

from mailsome.bootstrap import configure

configure()
django_application = get_asgi_application()


async def application(scope, receive, send):
    if scope["type"] != "lifespan":
        await django_application(scope, receive, send)
        return

    from jobs.pipeline import run

    await receive()  # lifespan.startup
    workers = run()
    await asyncio.to_thread(workers.__enter__)
    try:
        await send({"type": "lifespan.startup.complete"})
        await receive()  # lifespan.shutdown
    finally:
        await asyncio.to_thread(workers.__exit__, None, None, None)
    await send({"type": "lifespan.shutdown.complete"})
