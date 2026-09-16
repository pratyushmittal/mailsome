from django.core.asgi import get_asgi_application

from mailsome.bootstrap import configure

configure()
application = get_asgi_application()
