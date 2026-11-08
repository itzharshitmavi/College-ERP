from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('info', '0070_student_roll_bigint'),
    ]

    operations = [
        migrations.AddConstraint(
            model_name='feepayment',
            constraint=models.UniqueConstraint(
                fields=('method', 'reference'),
                condition=~models.Q(reference=''),
                name='uniq_fee_payment_method_reference',
            ),
        ),
    ]
