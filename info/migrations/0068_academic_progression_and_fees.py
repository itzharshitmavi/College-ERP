from decimal import Decimal
import django.core.validators

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


def repair_student_academic_sessions(apps, schema_editor):
    Student = apps.get_model('info', 'Student')
    PendingStudent = apps.get_model('info', 'PendingStudent')
    RejectedStudent = apps.get_model('info', 'RejectedStudent')
    Teacher = apps.get_model('info', 'Teacher')
    PendingTeacher = apps.get_model('info', 'PendingTeacher')
    RejectedTeacher = apps.get_model('info', 'RejectedTeacher')
    Course = apps.get_model('info', 'Course')
    Session = apps.get_model('info', 'Session')
    Subject = apps.get_model('info', 'Subject')

    def normalise_status(model):
        for row in model.objects.all().only('pk', 'status').iterator():
            current = (row.status or '').strip().lower()
            mapped = {
                'approved': 'Approved',
                'pending': 'Pending',
                'rejected': 'Rejected',
            }.get(current)
            if mapped and row.status != mapped:
                model.objects.filter(pk=row.pk).update(status=mapped)

    for model in (Student, Teacher):
        normalise_status(model)

    def canonical_name(raw_name):
        parts = [part.strip() for part in str(raw_name or '').replace('/', '-').split('-') if part.strip()]
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            return str(raw_name or '').strip()
        start, end = int(parts[0]), int(parts[1])
        if len(parts[1]) == 2 and len(parts[0]) == 4:
            end += (start // 100) * 100
            if end < start:
                end += 100
        return f'{start}-{end}'

    def sessions_for(admission_year, duration):
        result = []
        existing_sessions = list(Session.objects.all())
        for offset in range(max(1, int(duration or 4))):
            start = int(admission_year) + offset
            name = f'{start}-{start + 1}'
            session = next((item for item in existing_sessions if canonical_name(item.name) == name), None)
            if session is None:
                session, _ = Session.objects.get_or_create(
                    name=name,
                    defaults={'is_current': False},
                )
                existing_sessions.append(session)
            result.append(session)
        return result

    for student in Student.objects.select_related('course', 'dept').all().iterator():
        if student.admission_year is None:
            continue
        duration = student.course.duration_years if student.course_id else None
        if not duration:
            continue
        cohort_sessions = sessions_for(student.admission_year, duration)
        semester = max(1, min(int(student.current_semester or 1), max(2, int(duration or 4) * 2)))
        session_index = min((semester - 1) // 2, len(cohort_sessions) - 1)
        current_session = cohort_sessions[session_index]
        Student.objects.filter(pk=student.pk).update(
            current_semester=semester,
            current_session_id=current_session.pk,
        )
        student.sessions.set(cohort_sessions)
        if student.course_id and student.dept_id:
            matching = Subject.objects.filter(
                course_id=student.course_id,
                department_id=student.dept_id,
                session_id=current_session.pk,
                semester=semester,
            )
            student.subjects.set(matching)

    for model in (PendingStudent, RejectedStudent):
        for student in model.objects.select_related('course').all().iterator():
            if student.admission_year is None:
                continue
            duration = student.course.duration_years if student.course_id else None
            if not duration:
                continue
            cohort_sessions = sessions_for(student.admission_year, duration)
            semester = max(1, min(int(student.current_semester or 1), max(2, int(duration or 4) * 2)))
            current_session = cohort_sessions[min((semester - 1) // 2, len(cohort_sessions) - 1)]
            model.objects.filter(pk=student.pk).update(
                current_semester=semester,
                current_session_id=current_session.pk,
            )
            student.sessions.set(cohort_sessions)

    # Statuses in the approval workflow are intentionally case-normalised.
    for model in (PendingTeacher, RejectedTeacher):
        if any(field.name == 'status' for field in model._meta.fields):
            normalise_status(model)


class Migration(migrations.Migration):
    dependencies = [
        ('info', '0067_studentcounselingrequest'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='AcademicProgressionLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('from_semester', models.PositiveSmallIntegerField()),
                ('to_semester', models.PositiveSmallIntegerField()),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('from_session', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='progressions_from', to='info.session')),
                ('performed_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='academic_progressions', to='info.student')),
                ('to_session', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='progressions_to', to='info.session')),
            ],
            options={'ordering': ['-created_at'], 'unique_together': {('student', 'from_semester', 'to_semester')}},
        ),
        migrations.CreateModel(
            name='FeeStructure',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('semester', models.PositiveSmallIntegerField(choices=[(1, 'Semester 1'), (2, 'Semester 2'), (3, 'Semester 3'), (4, 'Semester 4'), (5, 'Semester 5'), (6, 'Semester 6'), (7, 'Semester 7'), (8, 'Semester 8')])),
                ('fee_head', models.CharField(max_length=100)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal('0.01'))])),
                ('due_date', models.DateField(blank=True, null=True)),
                ('is_active', models.BooleanField(default=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('course', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fee_structures', to='info.course')),
                ('department', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fee_structures', to='info.dept')),
                ('session', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='fee_structures', to='info.session')),
            ],
            options={'ordering': ['course__name', 'department__name', 'semester', 'fee_head'], 'unique_together': {('course', 'department', 'session', 'semester', 'fee_head')}},
        ),
        migrations.CreateModel(
            name='StudentFeeInvoice',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('semester', models.PositiveSmallIntegerField(choices=[(1, 'Semester 1'), (2, 'Semester 2'), (3, 'Semester 3'), (4, 'Semester 4'), (5, 'Semester 5'), (6, 'Semester 6'), (7, 'Semester 7'), (8, 'Semester 8')])),
                ('invoice_number', models.CharField(blank=True, max_length=50, unique=True)),
                ('issue_date', models.DateField(default=django.utils.timezone.localdate)),
                ('due_date', models.DateField(blank=True, null=True)),
                ('total_amount', models.DecimalField(decimal_places=2, default=Decimal('0.00'), max_digits=10)),
                ('status', models.CharField(choices=[('Pending', 'Pending'), ('Partially Paid', 'Partially Paid'), ('Paid', 'Paid'), ('Cancelled', 'Cancelled')], default='Pending', max_length=20)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='created_fee_invoices', to=settings.AUTH_USER_MODEL)),
                ('session', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='fee_invoices', to='info.session')),
                ('student', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='fee_invoices', to='info.student')),
            ],
            options={'ordering': ['-issue_date', '-id'], 'unique_together': {('student', 'session', 'semester')}},
        ),
        migrations.CreateModel(
            name='StudentFeeInvoiceItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('fee_head', models.CharField(max_length=100)),
                ('description', models.CharField(blank=True, max_length=255)),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal('0.01'))])),
                ('invoice', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='info.studentfeeinvoice')),
            ],
            options={'ordering': ['fee_head'], 'unique_together': {('invoice', 'fee_head')}},
        ),
        migrations.CreateModel(
            name='FeePayment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('amount', models.DecimalField(decimal_places=2, max_digits=10, validators=[django.core.validators.MinValueValidator(Decimal('0.01'))])),
                ('method', models.CharField(choices=[('Razorpay', 'Razorpay'), ('Cash', 'Cash'), ('UPI', 'UPI'), ('Bank Transfer', 'Bank Transfer'), ('Cheque', 'Cheque')], max_length=20)),
                ('status', models.CharField(choices=[('Created', 'Created'), ('Successful', 'Successful'), ('Failed', 'Failed'), ('Refunded', 'Refunded')], default='Created', max_length=15)),
                ('reference', models.CharField(blank=True, help_text='Bank/UPI/receipt reference for offline payments', max_length=128)),
                ('gateway_order_id', models.CharField(blank=True, max_length=100, null=True, unique=True)),
                ('gateway_payment_id', models.CharField(blank=True, max_length=100, null=True, unique=True)),
                ('gateway_signature', models.CharField(blank=True, max_length=128)),
                ('gateway_payload', models.JSONField(blank=True, default=dict)),
                ('verified_at', models.DateTimeField(blank=True, null=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='recorded_fee_payments', to=settings.AUTH_USER_MODEL)),
                ('invoice', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='payments', to='info.studentfeeinvoice')),
            ],
            options={'ordering': ['-created_at']},
        ),
        migrations.RunPython(repair_student_academic_sessions, migrations.RunPython.noop),
    ]
