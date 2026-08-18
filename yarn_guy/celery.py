"""Celery application instance for floward_clone."""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "yarn_guy.settings.dev")

app = Celery("yarn_guy")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
