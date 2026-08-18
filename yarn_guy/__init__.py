"""floward_clone project package; loads Celery app on Django startup."""

from __future__ import annotations

from floward_clone.celery import app as celery_app

__all__ = ("celery_app",)
