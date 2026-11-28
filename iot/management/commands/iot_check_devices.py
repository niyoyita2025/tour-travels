from django.core.management.base import BaseCommand

from iot import services


class Command(BaseCommand):
    help = ('Open alerts for devices that stopped reporting and for missing measurements. '
            'Run it every few minutes (for example with a scheduled job); it is safe to run often.')

    def handle(self, *args, **options):
        opened = services.check_devices()
        self.stdout.write(f'Device check complete. {opened} new alert(s).')
