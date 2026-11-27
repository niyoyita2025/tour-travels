#!/usr/bin/env bash
# Render build command: ./build.sh
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
# Creates/updates the tables in the PostgreSQL database named by DATABASE_URL.
python manage.py migrate --no-input

# First deploy only: load the starter destinations, tours and FAQs into the empty database.
if ! python manage.py shell -c "import sys; from Tour_travels.models import Destination; sys.exit(0 if Destination.objects.exists() else 1)"; then
  python manage.py seed_demo
fi

# Create the admin account from DJANGO_SUPERUSER_USERNAME / _EMAIL / _PASSWORD (skipped if it already exists).
if [ -n "${DJANGO_SUPERUSER_PASSWORD:-}" ]; then
  python manage.py createsuperuser --no-input || echo "Admin account already exists, skipping."
fi
