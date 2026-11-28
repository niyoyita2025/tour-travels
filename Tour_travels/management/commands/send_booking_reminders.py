from django.core.management.base import BaseCommand

from Tour_travels.reminders import send_due_reminders


class Command(BaseCommand):
    help = 'Email reminders for bookings whose tour starts within the reminder window set in the dashboard.'

    def handle(self, *args, **options):
        sent = send_due_reminders()
        self.stdout.write(self.style.SUCCESS(f'Sent {sent} reminder(s).'))
