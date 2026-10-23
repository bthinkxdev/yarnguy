"""Create an India Post barcode pool explicitly (pools are never created implicitly)."""

from django.core.management.base import BaseCommand, CommandError

from indiapost.models import IndiaPostBarcodePool

#the series India Post issues for the UAT (test) environment
UAT_PREFIX, UAT_START, UAT_END, UAT_SUFFIX = "ET", 21433001, 21434000, "IN"


class Command(BaseCommand):
    help = "Create an India Post barcode pool. Use --uat for the test-environment series."

    def add_arguments(self, parser):
        parser.add_argument("--uat", action="store_true", help="Use the UAT series ET21433001..ET21434000 IN.")
        parser.add_argument("--prefix", default=None)
        parser.add_argument("--start", type=int, default=None, help="First 8-digit serial number.")
        parser.add_argument("--end", type=int, default=None, help="Last 8-digit serial number.")
        parser.add_argument("--suffix", default=None)

    def handle(self, *args, **options):
        if options["uat"]:
            prefix, start, end, suffix = UAT_PREFIX, UAT_START, UAT_END, UAT_SUFFIX
        else:
            prefix, start, end, suffix = (options[k] for k in ("prefix", "start", "end", "suffix"))
            if None in (prefix, start, end, suffix):
                raise CommandError("Pass --uat, or all of --prefix --start --end --suffix.")
        if start > end:
            raise CommandError("--start must not be greater than --end.")

        pool, created = IndiaPostBarcodePool.objects.get_or_create(
            prefix=prefix, end_number=end, suffix=suffix, defaults={"current_number": start, "is_active": True}
        )
        state = "Created" if created else "Already exists:"
        self.stdout.write(
            self.style.SUCCESS(f"{state} pool {pool.prefix}{start:08d}x{pool.suffix} .. {end:08d} (next {pool.current_number}).")
        )
