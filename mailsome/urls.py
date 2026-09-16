from django.urls import include, path

from mailsome.views import static_file

urlpatterns = [
    path("", include("accounts.urls")),
    path("", include("inbox.urls")),
    path("", include("classifications.urls")),
    path("", include("jobs.urls")),
    # Local Uvicorn serves UI assets even with DEBUG=False; static() is DEBUG-only.
    # This view buffers small files for ASGI and only serves static/, never private data/.
    path("static/<path:path>", static_file),
]
