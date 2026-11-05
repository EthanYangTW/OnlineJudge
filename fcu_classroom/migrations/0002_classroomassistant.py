from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('fcu_classroom', '0001_initial'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='ClassroomAssistant',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('classroom', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                                related_name='assistants', to='fcu_classroom.classroom')),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE,
                                           related_name='fcu_classes_assisted', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'db_table': 'fcu_classroom_assistant',
                'unique_together': {('classroom', 'user')},
            },
        ),
    ]
