from django.db import models
from core.models import TimeStampedModel


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
        Allocates the next sequential barcode from the active pool.
        """
        pool = cls.objects.filter(is_active=True).first()
        if not pool:
            #create default UAT test range if none exists
            pool = cls.objects.create(
                prefix="ET",
                current_number=21433001,
                end_number=21434000,
                suffix="IN",
                is_active=True,
            )

        num = pool.current_number
        pool.current_number = num + 1
        pool.save(update_fields=["current_number", "updated_at"])

        #format 13-character barcode: prefix (2) + 9 digits or padded + suffix (2)
        #e.g. ET214330011IN -> 13 chars total
        #in the doc: "ET21433001XIN" has 2 prefix, 8 digits, 1 check/digit, 2 suffix = 13 chars.
        return f"{pool.prefix}{num:08d}0{pool.suffix}"[:13]


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
