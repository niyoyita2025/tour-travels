from django.db import migrations, models


def copy_destination_forward(apps, schema_editor):
    """Each tour's single destination becomes its first destination in the new list."""
    TourPackage = apps.get_model('Tour_travels', 'TourPackage')
    for tour in TourPackage.objects.exclude(destination=None):
        tour.destinations.add(tour.destination_id)


class Migration(migrations.Migration):

    dependencies = [
        ('Tour_travels', '0006_background_details_social'),
    ]

    operations = [
        migrations.AddField(
            model_name='tourpackage',
            name='destinations',
            field=models.ManyToManyField(blank=True, help_text='Every place this tour visits. The tour is offered on each of these destination pages.', related_name='packages', to='Tour_travels.destination'),
        ),
        migrations.RunPython(copy_destination_forward, migrations.RunPython.noop),
        migrations.RemoveField(
            model_name='tourpackage',
            name='destination',
        ),
    ]
