from django.core.management.base import BaseCommand

from Tour_travels.destination_tours import ensure_destination_tour
from Tour_travels.models import Destination, TourPackage


class Command(BaseCommand):
    help = ('Give every destination its bookable tour (created if missing) and open dates ahead for it. '
            'Safe to run again: existing tours and dates are kept.')

    def handle(self, *args, **options):
        created = 0
        for destination in Destination.objects.order_by('name'):
            if not TourPackage.objects.filter(auto_for=destination).exists():
                created += 1
            tour = ensure_destination_tour(destination)
            self.stdout.write(f'{tour.title} — open dates ready')
        self.stdout.write(self.style.SUCCESS(
            f'Done. {created} tour{"s" if created != 1 else ""} created; {Destination.objects.count()} destination(s) checked.'))
