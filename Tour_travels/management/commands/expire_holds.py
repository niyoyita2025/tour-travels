from django.core.management.base import BaseCommand

from Tour_travels.bookings import expire_stale_holds


class Command(BaseCommand):
    help = ('Release pending booking requests that were not confirmed in time, so their seats '
            'can be sold again. Run every 15 minutes from cron or Windows Task Scheduler.')

    def handle(self, *args, **options):
        count = expire_stale_holds()
        self.stdout.write(self.style.SUCCESS(f'Expired {count} pending booking request(s).'))
