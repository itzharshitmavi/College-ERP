from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("info", "0069_student_academic_status"),
    ]

    operations = [
        migrations.AlterField(
            model_name="student",
            name="roll",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="pendingstudent",
            name="roll",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="rejectedstudent",
            name="roll",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
    ]
