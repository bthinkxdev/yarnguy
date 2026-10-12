from django.db import migrations, models


class Migration(migrations.Migration):
    """Additive: a blank-by-default phone column. Pages fall back to whatsapp_number until set."""

    dependencies = [
        ("core", "0019_sitesettings_order_notification_email"),
    ]

    operations = [
        migrations.AddField(
            model_name="sitesettings",
            name="contact_phone",
            field=models.CharField(
                blank=True,
                help_text="Public contact number shown in the header, footer, contact page and policy pages. Include the country code, e.g. +919876543210. Falls back to the WhatsApp number if blank.",
                max_length=20,
                verbose_name="Phone number",
            ),
        ),
        migrations.AlterField(
            model_name="sitesettings",
            name="whatsapp_number",
            field=models.CharField(
                blank=True,
                help_text="Used for the WhatsApp chat button and invoices. Include the country code, e.g. +919876543210.",
                max_length=20,
            ),
        ),
    ]
