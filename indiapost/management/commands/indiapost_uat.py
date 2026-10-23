"""Run the India Post UAT scenarios against the configured environment and write the test report."""

from pathlib import Path
from urllib.parse import urlparse

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from indiapost.services import config_problems
from indiapost.uat import ERROR, FAIL, UAT_HOST, UATRunner, check_connection, write_report


class Command(BaseCommand):
    help = (
        "Exercise every India Post portal scenario against the configured environment, "
        "save the real requests/responses and write UAT_TEST_REPORT.md. "
        "Creates inactive UAT- test products/orders (cancelled afterwards) in the current database."
    )

    def add_arguments(self, parser):
        parser.add_argument("--output-dir", default=None, help="Evidence folder (default: uat_evidence/<timestamp>).")
        parser.add_argument("--destination-pincode", default="682001", help="Valid destination PIN used for bookings.")
        parser.add_argument("--receiver-mobile", default="9876543210", help="Valid 10-digit receiver mobile for bookings.")
        parser.add_argument("--only", nargs="+", metavar="UAT-NN", help="Run only these scenario ids.")
        parser.add_argument(
            "--skip-connection-check", action="store_true",
            help="Run the scenarios even if the server cannot be reached (every scenario will then fail).",
        )
        parser.add_argument(
            "--allow-production", action="store_true",
            help=f"Permit running against a host other than {UAT_HOST}. Books real articles - use with care.",
        )

    def handle(self, *args, **options):
        host = urlparse(settings.INDIA_POST_BASE_URL).netloc
        if host != UAT_HOST and not options["allow_production"]:
            raise CommandError(
                f"INDIA_POST_BASE_URL points at '{host or 'nothing'}', not the UAT server {UAT_HOST}. "
                "Refusing to run; pass --allow-production only if you really mean it."
            )
        problems = config_problems()
        if problems:
            raise CommandError("India Post is not configured: " + "; ".join(problems) + ".")

        if not options["skip_connection_check"]:
            connection = check_connection(settings.INDIA_POST_BASE_URL)
            if not connection.ok:
                raise CommandError(
                    f"Cannot reach India Post ({connection.stage.upper()}): {connection.detail}\n{connection.hint}"
                )
            self.stdout.write(self.style.SUCCESS(connection.detail))

        stamp = timezone.now().strftime("%Y%m%d-%H%M%S")
        output_dir = Path(options["output_dir"] or Path(settings.BASE_DIR) / "uat_evidence" / stamp)
        self.stdout.write(f"Running UAT scenarios against {host}; evidence -> {output_dir}")

        runner = UATRunner(
            output_dir=output_dir,
            destination_pincode=options["destination_pincode"],
            receiver_mobile=options["receiver_mobile"],
        )
        results = runner.run(only=options["only"])
        report = write_report(results, output_dir)

        for r in results:
            style = {"PASS": self.style.SUCCESS, "SKIPPED": self.style.WARNING}.get(r.status, self.style.ERROR)
            self.stdout.write(style(f"{r.id}  {r.status:<7} {r.name}"))
            if r.status in (FAIL, ERROR):
                self.stdout.write(f"        {r.actual}")
        self.stdout.write(f"\nReport: {report}")

        if any(r.status in (FAIL, ERROR) for r in results):
            raise CommandError("One or more UAT scenarios failed - see the report.")
