from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("info", "0068_academic_progression_and_fees")]

    operations = [
        migrations.AddField(
            model_name="student",
            name="academic_status",
            field=models.CharField(
                choices=[("Enrolled", "Enrolled"), ("Graduated", "Graduated"), ("Discontinued", "Discontinued")],
                default="Enrolled",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="student",
            name="graduated_at",
            field=models.DateField(blank=True, null=True),
        ),
    ]
