"""Celery application instance for floward_clone."""

from __future__ import annotations

import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "floward_clone.settings.dev")

app = Celery("floward_clone")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
