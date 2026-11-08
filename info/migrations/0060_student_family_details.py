from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('info', '0059_exammark'),
    ]

    operations = [
        migrations.AddField(
            model_name='student',
            name='family_address',
            field=models.TextField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='student',
            name='family_city',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='student',
            name='family_details_completed',
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name='student',
            name='family_pincode',
            field=models.CharField(blank=True, max_length=10, null=True),
        ),
        migrations.AddField(
            model_name='student',
            name='family_state',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='student',
            name='father_name',
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
        migrations.AddField(
            model_name='student',
            name='guardian_name',
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
        migrations.AddField(
            model_name='student',
            name='guardian_phone',
            field=models.CharField(blank=True, max_length=15, null=True),
        ),
        migrations.AddField(
            model_name='student',
            name='guardian_relation',
            field=models.CharField(blank=True, max_length=100, null=True),
        ),
        migrations.AddField(
            model_name='student',
            name='mother_name',
            field=models.CharField(blank=True, max_length=200, null=True),
        ),
    ]
