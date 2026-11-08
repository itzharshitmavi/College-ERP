from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('info', '0054_chatmessage_soft_delete'),
    ]

    operations = [
        migrations.CreateModel(
            name='ChatReadState',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('last_seen_message_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('user', models.OneToOneField(on_delete=models.deletion.CASCADE, related_name='chat_read_state', to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
