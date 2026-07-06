"""Permanently delete trashed items older than the configured retention.

Run periodically (e.g. daily via cron, see DEPLOYMENT.md):

    python manage.py purge_trash

The retention comes from the runtime setting ``trash_retention_days``. The
trash page also purges opportunistically when opened, so this command mainly
covers collections nobody visits.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from Collection_Management_System.models import Item
from Collection_Management_System.runtime_settings import get_setting


class Command(BaseCommand):
    help = 'Permanently delete trashed items past the retention period.'

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(days=get_setting('trash_retention_days'))
        expired = Item.all_objects.filter(deleted_at__lt=cutoff)
        count = 0
        for item in expired:
            item.purge()  # also removes the uploaded files from disk
            count += 1
        self.stdout.write(self.style.SUCCESS(f'{count} trashed item(s) purged.'))
