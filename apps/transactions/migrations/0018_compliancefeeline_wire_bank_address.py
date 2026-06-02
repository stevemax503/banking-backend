from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('transactions', '0017_compliance_payment_proof'),
    ]

    operations = [
        migrations.AddField(
            model_name='compliancefeeline',
            name='wire_bank_address',
            field=models.CharField(blank=True, default='', max_length=300),
        ),
    ]
