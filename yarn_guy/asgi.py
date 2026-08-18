"""ASGI config for floward_clone."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "floward_clone.settings.prod")

application = get_asgi_application()
