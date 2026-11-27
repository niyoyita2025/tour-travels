from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError

SMTP_BACKEND = 'django.core.mail.backends.smtp.EmailBackend'


class Command(BaseCommand):
    help = ('Send one test email with the current email settings, to confirm that Gmail sign-in and delivery '
            'work. Example: python manage.py send_test_email you@example.com')

    def add_arguments(self, parser):
        parser.add_argument('to', help='The address that should receive the test email.')

    def handle(self, *args, **options):
        to = options['to']
        if settings.EMAIL_BACKEND == SMTP_BACKEND and not (settings.EMAIL_HOST_USER and settings.EMAIL_HOST_PASSWORD):
            raise CommandError('Set EMAIL_HOST_USER and EMAIL_HOST_PASSWORD (a Google app password) in .env first.')
        self.stdout.write(f'Sending through {settings.EMAIL_HOST}:{settings.EMAIL_PORT} '
                          f'from {settings.DEFAULT_FROM_EMAIL} ...')
        try:
            send_mail('Explore Rwanda: test email',
                      'This is a test. If you can read it, email delivery is working.',
                      settings.DEFAULT_FROM_EMAIL, [to], fail_silently=False)
        except Exception as error:  # noqa: BLE001 - report any SMTP or network failure to the person running this
            raise CommandError(f'The email was not sent ({type(error).__name__}: {error}).') from None
        self.stdout.write(self.style.SUCCESS(f'Test email sent to {to}. Check the inbox and the spam folder.'))
