"""Run every India Post test and print a detailed report for production activation."""

from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from indiapost.postoffice_report import (
    DEFAULT_LABELS,
    LiveSection,
    render_text,
    run_automated_tests,
    run_staging_scenarios,
    write_outputs,
)


class Command(BaseCommand):
    help = (
        "Run all India Post test suites (and, from a machine India Post has whitelisted, the live staging "
        "scenarios), print the full test report and save it as INDIAPOST_TEST_REPORT.md. "
        "Exits non-zero if any executed test fails."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default=None, help="Where to write the report (default: postoffice_reports/<timestamp>).")
        parser.add_argument("--labels", nargs="+", default=DEFAULT_LABELS, help="Test labels to run (default: all India Post modules).")
        parser.add_argument(
            "--no-staging", action="store_true",
            help="Skip the live staging scenarios (the report then states they were not executed).",
        )
        parser.add_argument("--destination-pincode", default="682001", help="Valid destination PIN used for staging bookings.")
        parser.add_argument("--receiver-mobile", default="9876543210", help="Valid 10-digit receiver mobile for staging bookings.")
        parser.add_argument("--allow-production", action="store_true", help="Permit staging scenarios against a non-staging host.")

    def handle(self, *args, **options):
        output_dir = Path(options["output_dir"] or Path(settings.BASE_DIR) / "postoffice_reports" / timezone.now().strftime("%Y%m%d-%H%M%S"))
        self.stdout.write("Running the India Post automated test suites (this takes about a minute)...")
        records = run_automated_tests(options["labels"])

        if options["no_staging"]:
            live = LiveSection(False, "Skipped with --no-staging.")
        else:
            self.stdout.write("Running the staging scenarios against the India Post server...")
            live = run_staging_scenarios(
                output_dir,
                destination_pincode=options["destination_pincode"],
                receiver_mobile=options["receiver_mobile"],
                allow_production=options["allow_production"],
            )

        report = write_outputs(output_dir, records, live, labels=options["labels"])
        self.stdout.write(render_text(records, live))
        self.stdout.write(f"\nReport saved to: {report}")

        failed = [r for r in records if r.status in ("FAIL", "ERROR")]
        failed_live = [r for r in live.results if r.status in ("FAIL", "ERROR")]
        if failed or failed_live:
            raise CommandError(f"{len(failed)} automated test(s) and {len(failed_live)} staging scenario(s) failed - see the report.")
        if not records:
            raise CommandError("No tests were found for the given labels.")
