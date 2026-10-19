from django.db import models, transaction
from core.models import TimeStampedModel


def calculate_upu_s10_check_digit(serial_8_digits: int) -> int:
    """
    Computes the standard UPU S10 Modulo 11 check digit for a 13-character AWB.
    Weights: 8, 6, 4, 2, 3, 5, 9, 7.
    """
    digits_str = f"{serial_8_digits:08d}"[-8:]
    weights = [8, 6, 4, 2, 3, 5, 9, 7]
    weighted_sum = sum(int(d) * w for d, w in zip(digits_str, weights))
    remainder = weighted_sum % 11
    if remainder == 0:
        return 5
    elif remainder == 1:
        return 0
    else:
        return 11 - remainder


class IndiaPostBarcodePool(TimeStampedModel):
    """
    Tracks sequential allocation of India Post barcodes (AWB series).
    For UAT testing: ET21433001XIN to ET21434000XIN.
    """
    prefix = models.CharField(max_length=5, default="ET")
    current_number = models.PositiveIntegerField(default=21433001)
    end_number = models.PositiveIntegerField(default=21434000)
    suffix = models.CharField(max_length=5, default="IN")
    is_active = models.BooleanField(default=True)

    class Meta:
        verbose_name = "India Post Barcode Pool"
        verbose_name_plural = "India Post Barcode Pools"

    @classmethod
    def allocate_next_barcode(cls) -> str:
        """
        Atomically allocates the next sequential barcode from the active pool
        with UPU S10 check digit calculation and exhaustion prevention.
        """
        with transaction.atomic():
            pool = cls.objects.select_for_update().filter(is_active=True).first()
            if not pool:
                #create default UAT test range if none exists
                pool = cls.objects.create(
                    prefix="ET",
                    current_number=21433001,
                    end_number=21434000,
                    suffix="IN",
                    is_active=True,
                )

            if pool.current_number > pool.end_number:
                raise RuntimeError(
                    f"India Post barcode pool is exhausted ({pool.current_number} > {pool.end_number}). "
                    "Please configure a new barcode series in Django Admin."
                )

            num = pool.current_number
            pool.current_number = num + 1
            pool.save(update_fields=["current_number", "updated_at"])

            check_digit = calculate_upu_s10_check_digit(num)
            return f"{pool.prefix}{num:08d}{check_digit}{pool.suffix}"



class IndiaPostShipment(TimeStampedModel):
    """
    Stores India Post shipment details and barcode tracking numbers.
    """
    order = models.OneToOneField(
        "orders.Order",
        on_delete=models.CASCADE,
        related_name="indiapost_shipment",
        verbose_name="Order",
    )
    barcode_number = models.CharField(
        max_length=20,
        blank=True,
        null=True,
        unique=True,
        db_index=True,
        verbose_name="Barcode / AWB Number",
        help_text="13-character Article ID / Barcode assigned by India Post.",
    )
    article_type = models.CharField(
        max_length=50,
        default="SP_INLAND_PARCEL",
        verbose_name="Article Type",
        help_text="SP_INLAND_DOC, SP_INLAND_PARCEL, or BUSINESS_PARCEL.",
    )
    contract_id = models.CharField(
        max_length=20,
        blank=True,
        verbose_name="Contract ID",
    )
    batch_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Batch ID",
    )
    mail_booking_dom_id = models.CharField(
        max_length=100,
        blank=True,
        verbose_name="Mail Booking DOM ID",
    )
    tracking_status = models.CharField(
        max_length=100,
        default="Initiated",
        db_index=True,
        verbose_name="Tracking Status",
    )
    calculated_tariff = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0,
        verbose_name="Calculated Tariff (INR)",
    )
    label_pdf = models.FileField(
        upload_to="indiapost_labels/%Y/%m/",
        blank=True,
        null=True,
        verbose_name="Shipping Label (PDF)",
    )
    raw_booking_response = models.JSONField(
        default=dict,
        blank=True,
        verbose_name="Booking Response",
        help_text="Raw JSON response from India Post bulk booking API.",
    )
    last_status_at = models.DateTimeField(
        null=True,
        blank=True,
        verbose_name="Last Status At",
    )
    last_event_code = models.CharField(
        max_length=50,
        blank=True,
        verbose_name="Last Event Code",
    )
    last_event_description = models.CharField(
        max_length=255,
        blank=True,
        verbose_name="Last Event Description",
    )
    error_message = models.TextField(
        blank=True,
        null=True,
        verbose_name="Error Message / Logs",
    )

    class Meta:
        verbose_name = "India Post Shipment"
        verbose_name_plural = "India Post Shipments"
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"India Post Shipment for Order {self.order.order_number} ({self.barcode_number or 'No AWB'})"
