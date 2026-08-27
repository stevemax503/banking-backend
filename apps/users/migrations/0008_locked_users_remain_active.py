from django.db import migrations


def activate_locked_users(apps, schema_editor):
    CustomUser = apps.get_model('users', 'CustomUser')
    CustomUser.objects.filter(is_locked=True, is_active=False).update(is_active=True)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):
    dependencies = [
        ('users', '0007_customuser_compliance_fees_exempt'),
    ]

    operations = [
        migrations.RunPython(activate_locked_users, noop),
    ]
