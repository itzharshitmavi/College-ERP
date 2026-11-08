from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('info', '0058_exam_exam_time_exam_scheduled_students'),
    ]

    operations = [
        migrations.CreateModel(
            name='ExamMark',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('marks_obtained', models.PositiveSmallIntegerField(blank=True, null=True)),
                ('is_absent', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('entered_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='entered_exam_marks', to='info.teacher')),
                ('exam', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='marks_entries', to='info.exam')),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='exam_marks', to='info.student')),
            ],
            options={
                'ordering': ['student__roll', 'student__name'],
                'unique_together': {('exam', 'student')},
            },
        ),
    ]
