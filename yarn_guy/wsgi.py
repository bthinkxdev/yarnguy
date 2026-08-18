"""WSGI config for floward_clone."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yarn_guy.settings")

application = get_wsgi_application()
