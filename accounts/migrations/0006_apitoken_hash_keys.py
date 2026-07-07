# Store only the SHA-256 hash of API-token keys. Existing plain keys are
# hashed in place, so all issued tokens keep working after the upgrade.
import hashlib

from django.db import migrations, models


def hash_existing_keys(apps, schema_editor):
    ApiToken = apps.get_model('accounts', 'ApiToken')
    for token in ApiToken.objects.all():
        token.key_hash = hashlib.sha256(token.key.encode()).hexdigest()
        token.save(update_fields=['key_hash'])


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0005_webauthncredential'),
    ]

    operations = [
        migrations.AddField(
            model_name='apitoken',
            name='key_hash',
            field=models.CharField(editable=False, max_length=64, null=True),
        ),
        migrations.RunPython(hash_existing_keys, migrations.RunPython.noop),
        migrations.AlterField(
            model_name='apitoken',
            name='key_hash',
            field=models.CharField(editable=False, max_length=64, unique=True),
        ),
        migrations.RemoveField(
            model_name='apitoken',
            name='key',
        ),
    ]
