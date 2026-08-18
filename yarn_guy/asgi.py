"""ASGI config for yarn_guy."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yarn_guy.settings")

application = get_asgi_application()
