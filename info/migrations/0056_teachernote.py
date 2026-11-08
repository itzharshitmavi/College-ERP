from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('info', '0055_chatreadstate'),
    ]

    operations = [
        migrations.CreateModel(
            name='TeacherNote',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('title', models.CharField(max_length=200)),
                ('description', models.TextField(blank=True)),
                ('file', models.FileField(upload_to='teacher_notes/')),
                ('semester', models.PositiveSmallIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('course', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='info.course')),
                ('department', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='info.dept')),
                ('session', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='info.session')),
                ('subject', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='teacher_notes', to='info.subject')),
                ('teacher', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notes', to='info.teacher')),
            ],
            options={
                'ordering': ['-created_at'],
            },
        ),
    ]
