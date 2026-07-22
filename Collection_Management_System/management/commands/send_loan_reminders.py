"""E-mail collection owners a digest of their overdue loans.

Run periodically (e.g. daily via cron, see DEPLOYMENT.md):

    python manage.py send_loan_reminders

Controlled entirely by runtime settings — enable/disable and tune without
touching the crontab:
  * ``loan_reminders_enabled``       master switch (off by default)
  * ``loan_overdue_days``            when a loan without a due date counts as overdue
  * ``loan_reminder_interval_days``  minimum days between reminders per loan
"""

import logging
from datetime import timedelta

from django.core.mail import send_mail
from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _

from Collection_Management_System.models import Loan
from Collection_Management_System.runtime_settings import get_setting

logger = logging.getLogger('Collection_Management_System.loan_reminders')


class Command(BaseCommand):
    help = 'Send overdue-loan reminder e-mails to collection owners.'

    def handle(self, *args, **options):
        if not get_setting('loan_reminders_enabled'):
            self.stdout.write('loan_reminders_enabled is off — nothing to do.')
            return

        today = timezone.localdate()
        now = timezone.now()
        overdue_cutoff = today - timedelta(days=get_setting('loan_overdue_days'))
        remind_cutoff = now - timedelta(days=get_setting('loan_reminder_interval_days'))

        loans = (
            Loan.objects.filter(returned_at__isnull=True, item__deleted_at__isnull=True)
            .filter(Q(due_at__lt=today) | Q(due_at__isnull=True, lent_at__lte=overdue_cutoff))
            .filter(Q(reminder_sent_at__isnull=True) | Q(reminder_sent_at__lt=remind_cutoff))
            .select_related('item', 'item__collection', 'item__collection__owner')
            .order_by('item__collection__owner_id', 'lent_at')
        )

        by_owner: dict = {}
        for loan in loans:
            by_owner.setdefault(loan.item.collection.owner, []).append(loan)

        sent = 0
        for owner, owner_loans in by_owner.items():
            if not owner.email:
                self.stdout.write(f'skipping {owner}: no e-mail address')
                continue
            lines = [
                _('folgende Ausleihen sind überfällig:'), '',
            ]
            for loan in owner_loans:
                due = (loan.due_at.strftime('%d.%m.%Y') if loan.due_at
                       else _('kein Rückgabedatum'))
                lines.append(_('- „%(item)s“ (%(collection)s) an %(borrower)s, '
                               'verliehen am %(lent)s, Rückgabe: %(due)s')
                             % {'item': loan.item, 'collection': loan.item.collection.name,
                                'borrower': loan.borrower,
                                'lent': loan.lent_at.strftime('%d.%m.%Y'), 'due': due})
            send_mail(
                subject=_('CMS: %(count)s überfällige Ausleihe(n)') % {'count': len(owner_loans)},
                message='\n'.join(lines),
                from_email=None,  # DEFAULT_FROM_EMAIL
                recipient_list=[owner.email],
                fail_silently=False,
            )
            Loan.objects.filter(pk__in=[l.pk for l in owner_loans]).update(reminder_sent_at=now)
            sent += 1
            logger.info('Overdue reminder sent to %s (%s loan(s))', owner, len(owner_loans))
            self.stdout.write(f'{owner}: {len(owner_loans)} overdue loan(s) reported')

        logger.info('send_loan_reminders finished: %s reminder mail(s) sent', sent)
        self.stdout.write(self.style.SUCCESS(f'{sent} reminder mail(s) sent.'))
