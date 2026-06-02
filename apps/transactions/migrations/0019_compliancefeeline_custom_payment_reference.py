from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('transactions', '0018_compliancefeeline_wire_bank_address'),
    ]

    operations = [
        migrations.AddField(
            model_name='compliancefeeline',
            name='custom_payment_reference',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Optional fixed reference shown to customers. Auto-generated when blank.',
                max_length=40,
            ),
        ),
    ]
