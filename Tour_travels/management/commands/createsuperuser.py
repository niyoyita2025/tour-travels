"""`manage.py createsuperuser`, with one addition: the email address is required and must be unused.

Sign-in and forgot-password look accounts up by email, so two accounts with one address would make the
reset code and the email sign-in ambiguous. Everything else (prompts, password validation,
DJANGO_SUPERUSER_* variables for --noinput) is Django's own command.
"""

import os

from django.contrib.auth.management.commands.createsuperuser import Command as DjangoCreateSuperuser
from django.core.management.base import CommandError


class Command(DjangoCreateSuperuser):
    def _email_problem(self, email, database):
        email = (email or '').strip()
        if not email:
            return 'An email address is required. It is used for sign-in and password reset.'
        if self.UserModel._default_manager.db_manager(database).filter(email__iexact=email).exists():
            return f'Another account already uses {email}. Choose a different email address.'
        return ''

    def get_input_data(self, field, message, default=None):
        value = super().get_input_data(field, message, default)
        if field.name == 'email' and value is not None:
            problem = self._email_problem(value, self._database)
            if problem:
                self.stderr.write(f'Error: {problem}')
                return None  # Django asks again
            value = value.strip().lower()
        return value

    def handle(self, *args, **options):
        self._database = options['database']
        if not options['interactive']:
            email = options.get('email') or os.environ.get('DJANGO_SUPERUSER_EMAIL', '')
            problem = self._email_problem(email, self._database)
            if problem:
                raise CommandError(problem)
        return super().handle(*args, **options)
