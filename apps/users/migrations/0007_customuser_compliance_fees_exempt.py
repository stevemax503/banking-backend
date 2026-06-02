from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0006_admin_customer_assignments'),
    ]

    operations = [
        migrations.AddField(
            model_name='customuser',
            name='compliance_fees_exempt',
            field=models.BooleanField(
                default=False,
                help_text='When true, skip all compliance fee lines for international transfers and loan payouts.',
            ),
        ),
    ]
