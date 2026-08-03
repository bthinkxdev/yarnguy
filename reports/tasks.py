"""Celery tasks for the reports app."""

from __future__ import annotations

from celery import shared_task

from reports.services import aggregate_daily_reports


@shared_task(name="reports.tasks.aggregate_daily_reports")
def aggregate_daily_reports_task() -> dict[str, int]:
    """Nightly beat task to populate pre-aggregated report tables."""
    return aggregate_daily_reports()
