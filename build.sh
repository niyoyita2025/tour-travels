#!/usr/bin/env bash
# Render build command: ./build.sh
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
# Creates/updates the tables in the PostgreSQL database named by DATABASE_URL.
python manage.py migrate --no-input
