from django.apps import AppConfig


class TourTravelsConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'Tour_travels'

    def ready(self):
        from . import checks  # noqa: F401  (registers the email settings check)
