import time
import json
import datetime
import hashlib
import hmac
import base64
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from io import BytesIO
from datetime import date
from collections import defaultdict

from django.shortcuts import render, get_object_or_404, redirect
from django.http import HttpResponseRedirect, HttpResponseForbidden, HttpResponse, JsonResponse, FileResponse, Http404
from django.urls import reverse
from django.utils import timezone
from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.forms import PasswordChangeForm
from django.contrib.admin.views.decorators import staff_member_required
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST, require_GET
from django.utils.text import slugify
from django.utils.http import url_has_allowed_host_and_scheme
from django.db.models import Q, Count
from django.db import transaction, IntegrityError
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth.hashers import make_password, identify_hasher
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image

from pysendpulse.pysendpulse import PySendPulse
from CollegeERP.forms import (
    SpecialUpdateNoteForm,
    AdminProfileForm,
    TeacherProfileForm,
    StudentProfileForm,
    StudentFamilyDetailsForm,
    StudentCounselingRequestForm,
    FeeStructureForm,
)

from .models import (
    Dept, Class, RejectedStudent, Student, Course, Teacher, Assign, 
    PendingTeacher, PendingStudent, RejectedTeacher,
    time_slots, DAYS_OF_WEEK, AssignTime, StudentCourse, Marks, MarksClass, 
    Session, Subject, TimetableSlot, AttendanceRecord, StudentAttendance, 
    AttendanceSummary, SpecialUpdateNote, ChatMessage, NoticeReadState, ChatReadState, TeacherNote, Exam, ExamMark,
    TeacherLeave, StudentLeave, SubjectAssignment, AssignmentSubmission, HODAssignment, StudentDocument, StudentDocumentShare,
    StudentCounselingRequest, AcademicProgressionLog, FeeStructure, StudentFeeInvoice,
    StudentFeeInvoiceItem, FeePayment, canonical_academic_session_name,
)

User = get_user_model()

# Create your views here.


def is_superuser(user):
    return user.is_superuser


def semester_to_year(semester):
    try:
        semester_int = int(semester)
    except (TypeError, ValueError):
        return None

    if semester_int < 1:
        return None

    return (semester_int + 1) // 2


def get_active_hod_assignments_for_teacher(teacher):
    return HODAssignment.objects.filter(teacher=teacher, is_active=True).select_related('course', 'department')


def teacher_can_manage_subjects(teacher):
    return get_active_hod_assignments_for_teacher(teacher).exists()


def assign_subjects_to_student(student):
    """Synchronise the student's subjects with course, department, session and semester."""
    return student.assign_current_subjects()


def teacher_username_for_email(email):
    """Return a stable username that fits PendingTeacher.username (100 chars)."""
    normalized = (email or '').strip().lower()
    local_part = (normalized.split('@', 1)[0] or 'teacher')[:70]
    suffix = hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:10]
    return f'{local_part}_{suffix}'


def student_username_for(roll, email):
    """Use the institutional roll number, or a bounded stable legacy username."""
    if roll not in (None, ''):
        return str(roll)
    suffix = hashlib.sha256((email or '').strip().lower().encode('utf-8')).hexdigest()[:20]
    return f'student_{suffix}'


def cohort_session_for(admission_year, semester):
    """Resolve the correct annual session for an admission batch and semester."""
    if not admission_year:
        return None
    start_year = int(admission_year) + ((int(semester) - 1) // 2)
    return get_or_create_canonical_session(f'{start_year}-{start_year + 1}')


def set_user_password_from_pending(user, stored_password):
    """Accept both legacy raw pending passwords and newly hashed values."""
    if not stored_password:
        user.set_unusable_password()
    else:
        try:
            identify_hasher(stored_password)
        except ValueError:
            user.set_password(stored_password)
        else:
            user.password = stored_password
    user.save(update_fields=['password'])


def notify_registration_decision(request, email, name, subject, body):
    """Send notification without rolling back an already completed approval."""
    try:
        send_email_via_sendpulse(
            email,
            subject,
            body,
            request.user.email or settings.DEFAULT_FROM_EMAIL,
        )
        return True
    except Exception:
        # Email credentials/provider connectivity must not corrupt the approval workflow.
        return False


def normalize_session_name(raw_name):
    """Return canonical academic session name format like 2024-2025."""
    if raw_name is None:
        return ''
    name = str(raw_name).strip().replace('/', '-')
    return canonical_academic_session_name(name)


def get_or_create_canonical_session(session_name):
    """Reuse an existing session if logically equivalent; create only when missing."""
    canonical = normalize_session_name(session_name)
    if not canonical:
        return None

    direct = Session.objects.filter(name__iexact=canonical).first()
    if direct:
        return direct

    for existing in Session.objects.all():
        if normalize_session_name(existing.name).lower() == canonical.lower():
            return existing

    return Session.objects.create(name=canonical)


def get_session_start_year(session_obj):
    """Extract start year from a session like '2024-2025'."""
    if not session_obj or not session_obj.name:
        return None
    normalized = normalize_session_name(session_obj.name)
    parts = normalized.split('-')
    if len(parts) != 2:
        return None
    if not parts[0].isdigit():
        return None
    return int(parts[0])


def send_email_via_sendpulse(to_email, subject, text, from_email):
    sp = PySendPulse(
        settings.SENDPULSE_API_USER_ID,
        settings.SENDPULSE_API_SECRET,
        'FILE',
        settings.SENDPULSE_TOKEN_STORAGE_PATH
    )

    if not text or not text.strip():
        raise ValueError("Email text content cannot be empty")

    params = {
        "subject": subject,
        "from": {
            "name": "ERP Admin",
            "email": from_email
        },
        "to": [
            {"email": to_email}
        ],
        "text": text,
        "html": f"<html><body><p>{text}</p></body></html>"
    }

    result = sp.smtp_send_mail(params)
    return result





@user_passes_test(is_superuser)
def approve_requests(request):
    pending_students = Student.objects.filter(status='Pending')
    pending_teachers = Teacher.objects.filter(status='Pending')
    
    admin_uploaded_notes = SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')


    if request.method == 'POST':
        user_id = request.POST.get('user_id')
        role = request.POST.get('role')
        action = request.POST.get('action')

        if role == 'student':
            obj = Student.objects.get(user__id=user_id)
        else:
            obj = Teacher.objects.get(user__id=user_id)

        if action == 'approve':
            obj.status = 'Approved'
            obj.user.is_active = True
            obj.user.save()
            obj.save()
            # Assign subjects to student if role is student
            if role == 'student':
                assign_subjects_to_student(obj)
            messages.success(request, f"{role.title()} {obj.name} approved.")
        else:
            obj.user.delete()
            obj.delete()
            messages.info(request, f"{role.title()} {obj.name} rejected and deleted.")

        return redirect('approve_requests')

    return render(request, 'info/approve_requests.html', {
        'pending_students': pending_students,
        'pending_teachers': pending_teachers,
         
        'admin_uploaded_notes': admin_uploaded_notes,

    })


@login_required
@never_cache
def pending_requests_list(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("You are not authorized to view this page.")

    upload_form = SpecialUpdateNoteForm()
    pending_teachers = PendingTeacher.objects.all()
    pending_students = PendingStudent.objects.all()
    sessions = Session.objects.order_by('-name')
    current_session = Session.objects.filter(is_current=True).first()
    current_session_id = current_session.id if current_session else ''
    courses = Course.objects.order_by('name')
    departments = Dept.objects.order_by('name')
    approved_teachers = Teacher.objects.filter(status__iexact='Approved').select_related('course', 'dept').order_by('name')
    hod_assignments = HODAssignment.objects.select_related(
        'teacher', 'course', 'department'
    ).order_by('course__name', 'department__name', 'year')
    admission_years = Student.objects.filter(
        status__iexact='Approved',
        admission_year__isnull=False,
    ).order_by('admission_year').values_list('admission_year', flat=True).distinct()
    course_filter_meta = {}
    for course in courses:
        max_semesters = 8
        if course.duration_years:
            max_semesters = min(8, int(course.duration_years) * 2)

        years = list(
            Student.objects.filter(
                status__iexact='Approved',
                course=course,
                admission_year__isnull=False,
            ).order_by('admission_year').values_list('admission_year', flat=True).distinct()
        )

        related_sessions = Session.objects.filter(
            Q(students__course=course) | Q(pending_students__course=course) | Q(subjects__course=course)
        ).distinct().order_by('-name')

        course_filter_meta[str(course.id)] = {
            'max_semesters': max_semesters,
            'admission_years': years,
            'sessions': [{'id': s.id, 'name': s.name} for s in related_sessions],
        }
    
    admin_uploaded_notes = SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')


    return render(request, 'info/approve_request.html', {
        'upload_form': upload_form,
        'pending_teachers': pending_teachers,
        'pending_students': pending_students,
        'sessions': sessions,
        'current_session': current_session,
        'current_session_id': current_session_id,
        'courses': courses,
        'departments': departments,
        'approved_teachers': approved_teachers,
        'hod_assignments': hod_assignments,
        'hod_year_choices': HODAssignment.YEAR_CHOICES,
        'admission_years': admission_years,
        'course_filter_meta_json': json.dumps(course_filter_meta),
        'timestamp': time.time(),
         
        'admin_uploaded_notes': admin_uploaded_notes,

    })
@login_required
@user_passes_test(is_superuser)
def pending_rejected_requests_list(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("You are not authorized to view this page.")

    upload_form = SpecialUpdateNoteForm()
    rejected_teachers = RejectedTeacher.objects.select_related('dept', 'course').order_by('name')
    rejected_students = RejectedStudent.objects.select_related('department', 'course').order_by('name')

    admin_uploaded_notes = SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')

    return render(request, 'info/rejected_request.html', {
        'upload_form': upload_form,
        'rejected_teachers': rejected_teachers,
        'rejected_students': rejected_students,
        'timestamp': time.time(),
         
        'admin_uploaded_notes': admin_uploaded_notes,

    })
@login_required
@never_cache
def approve_request_view(request):
    # Keep legacy route functional but always serve the canonical admin dashboard.
    return redirect('pending_requests_list')

def _approve_student_request_record(pending, actor):
    """Approve a pending/rejected student using one canonical cohort rule."""
    email = (pending.email or '').strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        raise ValidationError('The student email address is invalid.')
    if not pending.admission_year or not 2000 <= int(pending.admission_year) <= 2100 or not pending.course_id or not pending.course.duration_years:
        raise ValidationError('Student admission year and course duration must be configured before approval.')
    max_semesters = min(8, int(pending.course.duration_years) * 2)
    semester = int(pending.current_semester or 1)
    if semester < 1 or semester > max_semesters:
        raise ValidationError('The student semester is outside the selected course duration.')
    if not pending.department_id or not _course_allows_department(pending.course, pending.department):
        raise ValidationError('The student department is not assigned to the selected course.')
    existing_students = Student.objects.filter(email__iexact=email)
    if pending.user_id:
        existing_students = existing_students.exclude(user_id=pending.user_id)
    if existing_students.exists():
        raise ValidationError('A student with this email already exists. Resolve the duplicate before approval.')
    if pending.roll is None or not 1 <= int(pending.roll) <= 9223372036854775807:
        raise ValidationError('The student roll number must be a positive 64-bit integer.')
    existing_rolls = Student.objects.filter(roll=pending.roll)
    if pending.user_id:
        existing_rolls = existing_rolls.exclude(user_id=pending.user_id)
    if existing_rolls.exists():
        raise ValidationError('A student with this roll number already exists. Resolve the duplicate before approval.')

    username = student_username_for(pending.roll, email)
    with transaction.atomic():
        user = pending.user if pending.user_id else User.objects.filter(username=username).first()
        if user is None:
            user = User.objects.create_user(username=username, email=email, password=None)
        else:
            if user.email and user.email.strip().lower() != email:
                raise ValidationError('The selected roll number is already linked to another user account.')
            if User.objects.filter(username=username).exclude(pk=user.pk).exists():
                raise ValidationError('The student roll number is already used as another account username.')
            user.username = username
            user.email = email
            user.save(update_fields=['username', 'email'])
        set_user_password_from_pending(user, pending.password)

        current_session = cohort_session_for(pending.admission_year, semester)
        if current_session is None:
            raise ValidationError('Could not resolve the academic session for this admission batch.')
        student, _ = Student.objects.get_or_create(user=user, defaults={
            'name': pending.name,
            'roll': pending.roll,
            'email': email,
            'dept': pending.department,
            'course': pending.course,
            'admission_year': pending.admission_year,
            'current_semester': semester,
            'current_session': current_session,
        })
        student.name = pending.name
        student.roll = pending.roll
        student.email = email
        student.dept = pending.department
        student.course = pending.course
        student.admission_year = pending.admission_year
        student.sex = pending.sex
        student.phone = pending.phone
        student.dob = pending.dob
        student.photo = pending.photo
        student.status = 'Approved'
        student.academic_status = Student.ACADEMIC_STATUS_ENROLLED
        student.graduated_at = None
        student.current_semester = semester
        student.current_session = current_session
        student.save()

        cohort_sessions = generate_sessions_for_admission(
            int(pending.admission_year), int(pending.course.duration_years)
        )
        student.sessions.set(cohort_sessions)
        assigned_count = assign_subjects_to_student(student)
        pending.delete()

    return student, assigned_count, email


@login_required
@user_passes_test(is_superuser)
@require_POST
def approve_student(request, pk):
    pending = get_object_or_404(PendingStudent, pk=pk)
    try:
        student, assigned_count, email = _approve_student_request_record(pending, request.user)
    except (ValidationError, IntegrityError) as exc:
        detail = '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'A conflicting account or student record already exists.'
        messages.error(request, detail)
        return redirect('pending_requests_list')

    email_sent = notify_registration_decision(
        request, email, student.name, 'Student Registration Approved',
        f'Hello {student.name}, your registration has been approved. Your current semester is {student.current_semester}.',
    )
    messages.success(request, f'Student approved successfully. {assigned_count} current subject(s) assigned.')
    if not email_sent:
        messages.warning(request, 'Approval is complete, but the email could not be sent. Configure SendPulse credentials to enable email notifications.')
    return redirect('pending_requests_list')


@login_required
@user_passes_test(is_superuser)
@require_POST
def approve_rejected_student(request, pk):
    pending = get_object_or_404(RejectedStudent, pk=pk)
    try:
        student, _assigned_count, email = _approve_student_request_record(pending, request.user)
    except (ValidationError, IntegrityError) as exc:
        detail = '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'A conflicting account or student record already exists.'
        messages.error(request, detail)
        return redirect('rejected_request_landing')

    email_sent = notify_registration_decision(
        request, email, student.name, 'Student Registration Approved',
        f'Hello {student.name}, your registration has been approved.',
    )
    messages.success(request, 'Rejected student request approved successfully.')
    if not email_sent:
        messages.warning(request, 'Approval is complete, but the email could not be sent. Configure SendPulse credentials to enable email notifications.')
    return redirect('rejected_request_landing')




@login_required
@user_passes_test(is_superuser)
@require_POST
def reject_student(request, pk):
    pending = get_object_or_404(PendingStudent, pk=pk)
    with transaction.atomic():
        rejected = RejectedStudent.objects.create(
            name=pending.name,
            email=pending.email,
            department=pending.department,
            roll=pending.roll,
            phone=pending.phone,
            sex=pending.sex,
            password=pending.password,
            dob=pending.dob,
            photo=pending.photo,
            course=pending.course,
            admission_year=pending.admission_year,
            current_semester=pending.current_semester,
            current_session=pending.current_session,
            user=pending.user,
        )
        rejected.sessions.set(pending.sessions.all())
        if pending.user_id:
            pending.user = None
            pending.save(update_fields=['user'])
        pending.delete()
    notify_registration_decision(
        request, pending.email, pending.name, 'Student Registration Rejected',
        f'Hello {pending.name}, your student registration request was rejected.',
    )
    messages.info(request, 'Student request rejected and archived.')
    return redirect('pending_requests_list')


def _approve_teacher_request_record(pending):
    """Activate a pending or rejected teacher using one validated account path."""
    email = (pending.email or '').strip().lower()
    try:
        validate_email(email)
    except ValidationError:
        raise ValidationError('The teacher email address is invalid.')
    if not pending.course_id or not pending.dept_id:
        raise ValidationError('Teacher course and department must be selected before approval.')
    if not _course_allows_department(pending.course, pending.dept):
        raise ValidationError('The teacher department is not assigned to the selected course.')
    existing_teachers = Teacher.objects.filter(email__iexact=email)
    if pending.user_id:
        existing_teachers = existing_teachers.exclude(user_id=pending.user_id)
    if existing_teachers.exists():
        raise ValidationError('A teacher with this email already exists.')

    username = (
        pending.username
        if pending.username and len(pending.username) <= 100
        else teacher_username_for_email(email)
    )
    with transaction.atomic():
        user = pending.user if pending.user_id else User.objects.filter(username=username).first()
        if user is not None and user.email and user.email.strip().lower() != email:
            raise ValidationError('The proposed teacher username is already linked to another user account.')
        if user is None:
            user = User.objects.create_user(username=username, email=email, password=None)
        else:
            if User.objects.filter(username=username).exclude(pk=user.pk).exists():
                raise ValidationError('The teacher username is already in use.')
            user.username = username
            user.email = email
            user.save(update_fields=['username', 'email'])
        set_user_password_from_pending(user, pending.password)

        teacher, _ = Teacher.objects.get_or_create(user=user, defaults={
            'name': pending.name,
            'email': email,
            'dept': pending.dept,
            'course': pending.course,
        })
        if Teacher.objects.filter(email__iexact=email).exclude(pk=teacher.pk).exists():
            raise ValidationError('A teacher record with this email already exists.')
        teacher.name = pending.name
        teacher.email = email
        teacher.dept = pending.dept
        teacher.course = pending.course
        teacher.phone = pending.phone
        teacher.sex = pending.sex
        teacher.dob = pending.dob
        teacher.photo = pending.photo
        teacher.status = 'Approved'
        teacher.save()
        if pending.user_id:
            pending.user = None
            pending.save(update_fields=['user'])
        pending.delete()

    return teacher, email


@login_required
@user_passes_test(is_superuser)
@require_POST
def approve_teacher(request, pk):
    pending = get_object_or_404(PendingTeacher, pk=pk)
    try:
        teacher, email = _approve_teacher_request_record(pending)
    except (ValidationError, IntegrityError) as exc:
        detail = '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'A conflicting teacher account already exists.'
        messages.error(request, detail)
        return redirect('pending_requests_list')

    email_sent = notify_registration_decision(
        request, email, teacher.name, 'Teacher Registration Approved',
        f'Hello {teacher.name}, your teacher registration has been approved.',
    )
    messages.success(request, 'Teacher approved successfully.')
    if not email_sent:
        messages.warning(request, 'Approval is complete, but the email could not be sent. Configure SendPulse credentials to enable email notifications.')
    return redirect('pending_requests_list')


@login_required
@user_passes_test(is_superuser)
@require_POST
def approve_rejected_teacher(request, pk):
    pending = get_object_or_404(RejectedTeacher, pk=pk)
    try:
        teacher, email = _approve_teacher_request_record(pending)
    except (ValidationError, IntegrityError) as exc:
        detail = '; '.join(exc.messages) if isinstance(exc, ValidationError) else 'A conflicting teacher account already exists.'
        messages.error(request, detail)
        return redirect('rejected_request_landing')

    email_sent = notify_registration_decision(
        request, email, teacher.name, 'Teacher Registration Approved',
        f'Hello {teacher.name}, your teacher registration has been approved.',
    )
    messages.success(request, 'Rejected teacher request approved successfully.')
    if not email_sent:
        messages.warning(request, 'Approval is complete, but the email could not be sent. Configure SendPulse credentials to enable email notifications.')
    return redirect('rejected_request_landing')


@login_required
@user_passes_test(is_superuser)
@require_POST
def reject_teacher(request, pk):
    pending = get_object_or_404(PendingTeacher, pk=pk)
    with transaction.atomic():
        RejectedTeacher.objects.create(
            username=pending.username,
            name=pending.name,
            email=pending.email,
            dept=pending.dept,
            user=pending.user,
            phone=pending.phone,
            password=pending.password,
            dob=pending.dob,
            sex=pending.sex,
            photo=pending.photo,
            course=pending.course,
        )
        if pending.user_id:
            pending.user = None
            pending.save(update_fields=['user'])
        pending.delete()
    notify_registration_decision(
        request, pending.email, pending.name, 'Teacher Registration Rejected',
        f'Hello {pending.name}, your teacher registration request was rejected.',
    )
    messages.info(request, 'Teacher request rejected and archived.')
    return redirect('pending_requests_list')


@never_cache
@login_required
def index(request):
    if request.user.is_superuser:
        # Always use the canonical admin dashboard view so required context
        # (sessions, courses, filters metadata) is consistently available.
        return redirect('pending_requests_list')
    elif request.user.is_teacher:
        return redirect('teacher_home')
    elif request.user.is_student:
        if not getattr(request.user.student, 'family_details_completed', False):
            return redirect('student_first_login_details')
        return render(request, 'info/homepage.html')
    return render(request, 'info/logout.html')


@login_required
def student_first_login_details(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can access this page.')

    student = request.user.student
    if student.family_details_completed:
        return redirect('index')

    if request.method == 'POST':
        form = StudentFamilyDetailsForm(request.POST, instance=student)
        if form.is_valid():
            updated_student = form.save(commit=False)
            updated_student.family_details_completed = True
            updated_student.save()
            messages.success(request, 'Family details saved successfully.')
            return redirect('index')
        messages.error(request, 'Please complete all required family details.')
    else:
        form = StudentFamilyDetailsForm(instance=student)

    return render(request, 'info/student_first_login_details.html', {'form': form})


@login_required()
def attendance(request, stud_id):
    if not hasattr(request.user, 'student'):
        messages.error(request, 'Only students can view this page.')
        return redirect('home')

    student = request.user.student

    # Student can only view their own attendance; normalize URL back to their roll.
    allowed_ids = {str(student.id)}
    if student.roll is not None:
        allowed_ids.add(str(student.roll))
    if str(stud_id) not in allowed_ids:
        canonical_id = student.roll if student.roll is not None else student.id
        return redirect('attendance', stud_id=canonical_id)

    selected_session = request.GET.get('session', '')
    selected_semester = request.GET.get('semester', '')
    filters_applied = bool(selected_session and selected_semester)

    base_records = StudentAttendance.objects.filter(
        student=student,
        attendance_record__session_held=True,
    )
    sessions = base_records.values_list(
        'attendance_record__timetable_slot__session_id',
        'attendance_record__timetable_slot__session__name',
    ).distinct().order_by('-attendance_record__timetable_slot__session__name')
    semesters = base_records.values_list(
        'attendance_record__timetable_slot__semester',
        flat=True,
    ).distinct().order_by('attendance_record__timetable_slot__semester')

    semester_attendance_list = []
    if filters_applied:
        grouped_rows = base_records.filter(
            attendance_record__timetable_slot__session_id=selected_session,
            attendance_record__timetable_slot__semester=selected_semester,
        ).values(
            'attendance_record__timetable_slot__session_id',
            'attendance_record__timetable_slot__session__name',
            'attendance_record__timetable_slot__semester',
        ).annotate(
            total_classes=Count('id'),
            classes_attended=Count('id', filter=Q(status=True)),
        ).order_by(
            '-attendance_record__timetable_slot__session__name',
            'attendance_record__timetable_slot__semester',
        )

        for row in grouped_rows:
            total_classes = row['total_classes'] or 0
            classes_attended = row['classes_attended'] or 0
            attendance_percentage = round((classes_attended / total_classes) * 100, 2) if total_classes else 0
            semester_attendance_list.append({
                'session_id': row['attendance_record__timetable_slot__session_id'],
                'session_name': row['attendance_record__timetable_slot__session__name'],
                'semester': row['attendance_record__timetable_slot__semester'],
                'total_classes': total_classes,
                'classes_attended': classes_attended,
                'attendance_percentage': attendance_percentage,
                'is_below_threshold': attendance_percentage < 75,
            })

    summary = {
       
        'total_classes': sum(item['total_classes'] for item in semester_attendance_list),
        'classes_attended': sum(item['classes_attended'] for item in semester_attendance_list),
       
    }

    return render(request, 'info/attendance.html', {
        'student': student,
        'semester_attendance_list': semester_attendance_list,
        'sessions': sessions,
        'semesters': semesters,
        'selected_session': selected_session,
        'selected_semester': selected_semester,
        'filters_applied': filters_applied,
        'summary': summary,
    })


@login_required()
def attendance_detail(request, stud_id, course_id):
    stud = get_object_or_404(Student, roll=stud_id)
    cr = get_object_or_404(Course, id=course_id)
    # Get attendance summary for this student and course
    summary = get_object_or_404(AttendanceSummary, student=stud, subject__course=cr)
    # Get all individual attendance records
    att_records = StudentAttendance.objects.filter(
        student=stud,
        attendance_record__session_held=True,
        attendance_record__timetable_slot__subject__course=cr
    ).order_by('-attendance_record__date')
    return render(request, 'info/att_detail.html', {'summary': summary, 'att_records': att_records, 'cr': cr})


@login_required()
def attendance_semester_detail(request, stud_id, session_id, semester):
    stud = get_object_or_404(Student, roll=stud_id)
    session_obj = get_object_or_404(Session, id=session_id)

    att_records = StudentAttendance.objects.filter(
        student=stud,
        attendance_record__session_held=True,
        attendance_record__timetable_slot__session_id=session_id,
        attendance_record__timetable_slot__semester=semester,
    ).select_related(
        'attendance_record__timetable_slot__subject',
        'attendance_record__timetable_slot__session',
    ).order_by('-attendance_record__date')

    total_classes = att_records.count()
    classes_attended = att_records.filter(status=True).count()
    absent_classes = total_classes - classes_attended
    attendance_percentage = round((classes_attended / total_classes) * 100, 2) if total_classes else 0

    summary = {
        'total_classes': total_classes,
        'classes_attended': classes_attended,
        'attendance_percentage': attendance_percentage,
        'absent_classes': absent_classes,
        'session_name': session_obj.name,
        'semester': semester,
        'semester_label': f'Semester {semester}',
    }

    return render(request, 'info/attendance_semester_detail.html', {
        'student': stud,
        'summary': summary,
        'att_records': att_records,
        'session_name': session_obj.name,
        'semester_label': f'Semester {semester}',
        'attendance_percentage': attendance_percentage,
        'total_classes': total_classes,
        'classes_attended': classes_attended,
        'absent_classes': absent_classes,
    })


# Teacher Views

@login_required
def t_clas(request, teacher_id, choice):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can access their class assignments.')
    teacher = request.user.teacher
    if teacher.id != teacher_id:
        return HttpResponseForbidden("You cannot view another teacher's assignments.")
    return render(request, 'info/t_clas.html', {'teacher1': teacher, 'choice': choice})


@login_required
def t_student(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can view department students.')
    teacher = request.user.teacher
    dept = teacher.dept
    admission_year = request.GET.get('admission_year')
    current_semester = request.GET.get('current_semester')
    sex = request.GET.get('sex')
    roll_no = request.GET.get('roll_no')
    session_id = request.GET.get('session')
    course_id = request.GET.get('course')

    students = Student.objects.filter(
        dept=dept, status__iexact='Approved',
        academic_status=Student.ACADEMIC_STATUS_ENROLLED,
    ).select_related('course', 'current_session').order_by('admission_year', 'roll')
    if admission_year:
        students = students.filter(admission_year=admission_year)
    if current_semester:
        students = students.filter(current_semester=current_semester)
    if sex:
        students = students.filter(sex=sex)
    if roll_no:
        students = students.filter(roll__icontains=roll_no)
    if session_id:
        students = students.filter(current_session_id=session_id)
    if course_id:
        students = students.filter(course_id=course_id)

    admission_years = Student.objects.filter(dept=dept).order_by('admission_year').values_list('admission_year', flat=True).distinct()
    semesters = Student.objects.filter(dept=dept).order_by('current_semester').values_list('current_semester', flat=True).distinct()

    return render(request, 'info/t_students.html', {
        'students': students, 'admission_years': admission_years, 'semesters': semesters,
        'department': dept,
    })


@login_required
def teacher_leave_apply(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can apply for leave.')

    teacher = request.user.teacher
    if request.method == 'POST':
        from_date_raw = (request.POST.get('from_date') or '').strip()
        to_date_raw = (request.POST.get('to_date') or '').strip()
        reason = (request.POST.get('reason') or '').strip()

        if not from_date_raw or not to_date_raw or not reason:
            messages.error(request, 'From date, to date, and reason are required.')
            return redirect('teacher_leave_apply')

        try:
            from_date_value = date.fromisoformat(from_date_raw)
            to_date_value = date.fromisoformat(to_date_raw)
        except ValueError:
            messages.error(request, 'Invalid date format. Please select valid dates.')
            return redirect('teacher_leave_apply')

        if from_date_value > to_date_value:
            messages.error(request, 'From date cannot be after to date.')
            return redirect('teacher_leave_apply')

        TeacherLeave.objects.create(
            teacher=teacher,
            from_date=from_date_value,
            to_date=to_date_value,
            reason=reason,
        )
        messages.success(request, 'Leave request submitted successfully.')
        return redirect('teacher_leave_apply')

    leave_requests = TeacherLeave.objects.filter(teacher=teacher)
    return render(request, 'info/teacher_leave_apply.html', {
        'leave_requests': leave_requests,
    })


@login_required
def admin_teacher_leave_requests(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Only admins can review leave requests.')

    selected_status = (request.GET.get('status') or TeacherLeave.STATUS_PENDING).strip()
    status_choices = [
        TeacherLeave.STATUS_PENDING,
        TeacherLeave.STATUS_APPROVED,
        TeacherLeave.STATUS_REJECTED,
        'All',
    ]
    if selected_status not in status_choices:
        selected_status = TeacherLeave.STATUS_PENDING

    leave_requests = TeacherLeave.objects.select_related(
        'teacher', 'teacher__dept', 'teacher__course', 'reviewed_by'
    ).order_by('-created_at')
    if selected_status != 'All':
        leave_requests = leave_requests.filter(status=selected_status)

    return render(request, 'info/admin_teacher_leaves.html', {
        'leave_requests': leave_requests,
        'selected_status': selected_status,
        'status_choices': status_choices,
    })


@login_required
@require_POST
def review_teacher_leave(request, leave_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Only admins can review leave requests.')

    leave_request = get_object_or_404(TeacherLeave, id=leave_id)
    if leave_request.status != TeacherLeave.STATUS_PENDING:
        messages.info(request, 'This leave request has already been reviewed.')
        return redirect('admin_teacher_leave_requests')

    decision = (request.POST.get('decision') or '').strip().lower()
    admin_remark = (request.POST.get('admin_remark') or '').strip()

    if decision not in ('approve', 'reject'):
        messages.error(request, 'Invalid decision.')
        return redirect('admin_teacher_leave_requests')

    leave_request.status = TeacherLeave.STATUS_APPROVED if decision == 'approve' else TeacherLeave.STATUS_REJECTED
    leave_request.admin_remark = admin_remark
    leave_request.reviewed_by = request.user
    leave_request.reviewed_at = timezone.now()
    leave_request.save(update_fields=['status', 'admin_remark', 'reviewed_by', 'reviewed_at', 'updated_at'])

    if leave_request.status == TeacherLeave.STATUS_APPROVED:
        messages.success(request, 'Leave request approved.')
    else:
        messages.success(request, 'Leave request rejected.')

    return redirect('admin_teacher_leave_requests')


@login_required
def student_leave_apply(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can apply for leave.')

    student = request.user.student
    student_year = semester_to_year(student.current_semester)
    hod_assignment = None
    if student.course_id and student.dept_id and student_year:
        hod_assignment = HODAssignment.objects.filter(
            course=student.course,
            department=student.dept,
            year=student_year,
            is_active=True,
            teacher__status__iexact='approved',
        ).select_related('teacher', 'course', 'department').first()

    if request.method == 'POST':
        from_date_raw = (request.POST.get('from_date') or '').strip()
        to_date_raw = (request.POST.get('to_date') or '').strip()
        reason = (request.POST.get('reason') or '').strip()

        if not from_date_raw or not to_date_raw or not reason:
            messages.error(request, 'From date, to date, and reason are required.')
            return redirect('student_leave_apply')

        if not hod_assignment:
            messages.error(request, 'No active HOD is assigned for your department and year. Please contact admin.')
            return redirect('student_leave_apply')

        try:
            from_date_value = date.fromisoformat(from_date_raw)
            to_date_value = date.fromisoformat(to_date_raw)
        except ValueError:
            messages.error(request, 'Invalid date format. Please select valid dates.')
            return redirect('student_leave_apply')

        if from_date_value > to_date_value:
            messages.error(request, 'From date cannot be after to date.')
            return redirect('student_leave_apply')

        StudentLeave.objects.create(
            student=student,
            teacher=hod_assignment.teacher,
            from_date=from_date_value,
            to_date=to_date_value,
            reason=reason,
        )
        messages.success(request, f'Leave request submitted to HOD {hod_assignment.teacher.name}.')
        return redirect('student_leave_apply')

    leave_requests = StudentLeave.objects.filter(student=student).select_related('teacher', 'teacher__dept', 'teacher__course')
    return render(request, 'info/student_leave_apply.html', {
        'leave_requests': leave_requests,
        'target_hod_assignment': hod_assignment,
        'student_year': student_year,
    })


@login_required
def teacher_student_leave_requests(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can review student leave requests.')

    teacher = request.user.teacher
    if not teacher_can_manage_subjects(teacher):
        return HttpResponseForbidden('Only assigned HOD teachers can access student leave requests.')
    selected_status = (request.GET.get('status') or StudentLeave.STATUS_PENDING).strip()
    status_choices = [
        StudentLeave.STATUS_PENDING,
        StudentLeave.STATUS_APPROVED,
        StudentLeave.STATUS_REJECTED,
        'All',
    ]
    if selected_status not in status_choices:
        selected_status = StudentLeave.STATUS_PENDING

    leave_requests = StudentLeave.objects.filter(teacher=teacher).select_related(
        'student', 'student__dept', 'student__course', 'reviewed_by'
    ).order_by('-created_at')
    if selected_status != 'All':
        leave_requests = leave_requests.filter(status=selected_status)

    return render(request, 'info/teacher_student_leaves.html', {
        'leave_requests': leave_requests,
        'selected_status': selected_status,
        'status_choices': status_choices,
    })


@login_required
@require_POST
def review_student_leave(request, leave_id):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can review student leave requests.')

    teacher = request.user.teacher
    if not teacher_can_manage_subjects(teacher):
        return HttpResponseForbidden('Only assigned HOD teachers can review student leave requests.')
    leave_request = get_object_or_404(StudentLeave, id=leave_id, teacher=teacher)
    if leave_request.status != StudentLeave.STATUS_PENDING:
        messages.info(request, 'This leave request has already been reviewed.')
        return redirect('teacher_student_leave_requests')

    decision = (request.POST.get('decision') or '').strip().lower()
    teacher_remark = (request.POST.get('teacher_remark') or '').strip()

    if decision not in ('approve', 'reject'):
        messages.error(request, 'Invalid decision.')
        return redirect('teacher_student_leave_requests')

    leave_request.status = StudentLeave.STATUS_APPROVED if decision == 'approve' else StudentLeave.STATUS_REJECTED
    leave_request.teacher_remark = teacher_remark
    leave_request.reviewed_by = request.user
    leave_request.reviewed_at = timezone.now()
    leave_request.save(update_fields=['status', 'teacher_remark', 'reviewed_by', 'reviewed_at', 'updated_at'])

    if leave_request.status == StudentLeave.STATUS_APPROVED:
        messages.success(request, 'Student leave request approved.')
    else:
        messages.success(request, 'Student leave request rejected.')

    return redirect('teacher_student_leave_requests')


@login_required
def student_counseling_support(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can raise counseling requests.')

    student = request.user.student
    student_year = semester_to_year(student.current_semester)
    hod_assignment = None
    if student.course_id and student.dept_id and student_year:
        hod_assignment = HODAssignment.objects.filter(
            course=student.course,
            department=student.dept,
            year=student_year,
            is_active=True,
            teacher__status__iexact='approved',
        ).select_related('teacher', 'course', 'department').first()

    if request.method == 'POST':
        if not hod_assignment:
            messages.error(request, 'No active HOD is assigned for your department and year. Please contact admin.')
            return redirect('student_counseling_support')

        form = StudentCounselingRequestForm(request.POST)
        if form.is_valid():
            counseling_request = form.save(commit=False)
            counseling_request.student = student
            counseling_request.hod_teacher = hod_assignment.teacher
            counseling_request.save()
            messages.success(request, f'Counseling request submitted to HOD {hod_assignment.teacher.name}.')
            return redirect('student_counseling_support')
    else:
        form = StudentCounselingRequestForm()

    counseling_requests = StudentCounselingRequest.objects.filter(student=student).select_related(
        'hod_teacher', 'hod_teacher__dept', 'hod_teacher__course', 'reviewed_by'
    )
    return render(request, 'info/student_counseling_support.html', {
        'form': form,
        'counseling_requests': counseling_requests,
        'target_hod_assignment': hod_assignment,
        'student_year': student_year,
    })


@login_required
def teacher_student_counseling_requests(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can review student counseling requests.')

    teacher = request.user.teacher
    if not teacher_can_manage_subjects(teacher):
        return HttpResponseForbidden('Only assigned HOD teachers can access student counseling requests.')

    selected_status = (request.GET.get('status') or StudentCounselingRequest.STATUS_PENDING).strip()
    status_choices = [
        StudentCounselingRequest.STATUS_PENDING,
        StudentCounselingRequest.STATUS_IN_PROGRESS,
        StudentCounselingRequest.STATUS_RESOLVED,
        StudentCounselingRequest.STATUS_REJECTED,
        'All',
    ]
    if selected_status not in status_choices:
        selected_status = StudentCounselingRequest.STATUS_PENDING

    counseling_requests = StudentCounselingRequest.objects.filter(hod_teacher=teacher).select_related(
        'student', 'student__dept', 'student__course', 'reviewed_by'
    ).order_by('-created_at')
    if selected_status != 'All':
        counseling_requests = counseling_requests.filter(status=selected_status)

    return render(request, 'info/teacher_student_counseling.html', {
        'counseling_requests': counseling_requests,
        'selected_status': selected_status,
        'status_choices': status_choices,
    })


@login_required
@require_POST
def review_student_counseling_request(request, request_id):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can review student counseling requests.')

    teacher = request.user.teacher
    if not teacher_can_manage_subjects(teacher):
        return HttpResponseForbidden('Only assigned HOD teachers can review student counseling requests.')

    counseling_request = get_object_or_404(StudentCounselingRequest, id=request_id, hod_teacher=teacher)
    if counseling_request.status in (StudentCounselingRequest.STATUS_RESOLVED, StudentCounselingRequest.STATUS_REJECTED):
        messages.info(request, 'This counseling request is already closed.')
        return redirect('teacher_student_counseling_requests')

    decision = (request.POST.get('decision') or '').strip().lower()
    hod_remark = (request.POST.get('hod_remark') or '').strip()

    if decision not in ('in_progress', 'resolve', 'reject'):
        messages.error(request, 'Invalid action.')
        return redirect('teacher_student_counseling_requests')

    if decision == 'in_progress':
        counseling_request.status = StudentCounselingRequest.STATUS_IN_PROGRESS
    elif decision == 'resolve':
        counseling_request.status = StudentCounselingRequest.STATUS_RESOLVED
    else:
        counseling_request.status = StudentCounselingRequest.STATUS_REJECTED

    counseling_request.hod_remark = hod_remark
    counseling_request.reviewed_by = request.user
    counseling_request.reviewed_at = timezone.now()
    counseling_request.save(update_fields=['status', 'hod_remark', 'reviewed_by', 'reviewed_at', 'updated_at'])

    messages.success(request, 'Counseling request updated successfully.')
    return redirect('teacher_student_counseling_requests')


def _build_attendance_modal_context(teacher, selected_session='', selected_semester='', selected_subject='', open_modal=False):
    timetable_slots = TimetableSlot.objects.filter(teacher=teacher).select_related('session', 'subject').order_by('session_id', 'semester', 'subject__name')

    options = []
    seen = set()
    for slot in timetable_slots:
        if not slot.session_id or not slot.subject_id or slot.semester is None:
            continue
        key = (slot.session_id, int(slot.semester), slot.subject_id)
        if key in seen:
            continue
        seen.add(key)
        options.append({
            'session_id': slot.session_id,
            'session_name': str(slot.session),
            'semester': int(slot.semester),
            'subject_id': slot.subject_id,
            'subject_name': slot.subject.name,
        })

    session_map = {}
    for option in options:
        session_map[option['session_id']] = option['session_name']

    session_list = [
        {'id': session_id, 'name': session_map[session_id]}
        for session_id in sorted(session_map.keys())
    ]

    return {
        'session_list': session_list,
        'sessions': session_list,
        'attendance_options_json': json.dumps(options),
        'open_attendance_modal': open_modal,
        'selected_session': str(selected_session) if selected_session is not None else '',
        'selected_semester': str(selected_semester) if selected_semester is not None else '',
        'selected_subject': str(selected_subject) if selected_subject is not None else '',
    }



@login_required()
def teacher_timetable(request, teacher_id):
    """View all timetable slots for a teacher to mark attendance"""
    teacher = get_object_or_404(Teacher, id=teacher_id)
    # Only allow teacher to view their own timetable
    if request.user.teacher != teacher:
        messages.error(request, 'You can only view your own timetable.')
        return redirect('home')
    
    timetable_slots = TimetableSlot.objects.filter(teacher=teacher).order_by('day', 'start_time')
    
    if not timetable_slots.exists():
        messages.info(request, 'You have no scheduled classes yet.')
    
    return render(request, 'info/teacher_timetable.html', {
        'timetable_slots': timetable_slots,
        'teacher': teacher
    })


@login_required()
@never_cache
def mark_attendance_page(request, timetable_slot_id):
    """Show attendance marking form for a specific timetable slot on a specific date"""
    timetable_slot = get_object_or_404(TimetableSlot, id=timetable_slot_id)
    teacher = request.user.teacher
    
    # Verify teacher owns this slot
    if timetable_slot.teacher != teacher:
        messages.error(request, 'You cannot mark attendance for this class.')
        return redirect('home')
    
    date_str = request.GET.get('date')
    if not date_str:
        date_obj = timezone.now().date()
    else:
        date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    
    # Get or create AttendanceRecord for this slot and date
    att_record, created = AttendanceRecord.objects.get_or_create(
        timetable_slot=timetable_slot,
        date=date_obj,
        defaults={'session_held': True}
    )
    
    # Get students enrolled in this subject
    students = Student.objects.filter(subjects=timetable_slot.subject).order_by('roll')

    start_time_display = timetable_slot.start_time.strftime('%I:%M %p').lstrip('0') if timetable_slot.start_time else ''
    end_time_display = timetable_slot.end_time.strftime('%I:%M %p').lstrip('0') if timetable_slot.end_time else ''
    time_range_display = f"{start_time_display} - {end_time_display}".strip(' -')
    
    # If no students found, try to auto-enroll from course/semester/department/session
    if not students.exists():
        filter_dict = {
            'course': timetable_slot.course,
            'dept': timetable_slot.department,
            'current_session': timetable_slot.session,  # Match by session too
        }
        
        if timetable_slot.semester:
            filter_dict['current_semester'] = timetable_slot.semester
        
        auto_students = Student.objects.filter(**filter_dict).exclude(status='Rejected').order_by('roll')
        
        if auto_students.exists():
            # Auto-enroll them in the subject
            for student in auto_students:
                student.subjects.add(timetable_slot.subject)
            students = auto_students
            messages.info(request, f'Auto-enrolled {students.count()} students in {timetable_slot.subject.name}')
    
    if not students.exists():
        messages.error(request, 'No students found for this class. Please check course, department, and semester settings.')
        return render(request, 'info/mark_attendance.html', {
            'att_record': att_record,
            'timetable_slot': timetable_slot,
            'students': [],
            'date': date_obj,
            'start_time_display': start_time_display,
            'end_time_display': end_time_display,
            'time_range_display': time_range_display,
        })
    
    # Get existing attendance entries
    existing_att = StudentAttendance.objects.filter(
        attendance_record=att_record
    )
    
    context = {
        'att_record': att_record,
        'timetable_slot': timetable_slot,
        'students': students,
        'existing_att': existing_att,
        'date': date_obj,
        'start_time_display': start_time_display,
        'end_time_display': end_time_display,
        'time_range_display': time_range_display,
    }
    return render(request, 'info/mark_attendance.html', context)


@login_required()
def t_class_date(request, assign_id):
    now = timezone.now()
    # Using TimetableSlot instead of old Assign model
    timetable_slot = get_object_or_404(TimetableSlot, id=assign_id)
    att_list = AttendanceRecord.objects.filter(
        timetable_slot=timetable_slot,
        date__lte=now.date()
    ).order_by('-date').prefetch_related('student_attendances')

    attendance_rows = []
    for record in att_list:
        if not record.session_held:
            status = 'cancelled'
        elif record.student_attendances.exists():
            status = 'marked'
        else:
            status = 'not_marked'
        attendance_rows.append({'record': record, 'status': status})

    # Shared history: all attendance records for teacher/admin users with status.
    can_view_all_pdfs = request.user.is_superuser or request.user.is_staff or hasattr(request.user, 'teacher')
    all_attendance_rows = []
    if can_view_all_pdfs:
        all_records = AttendanceRecord.objects.filter(
            date__lte=now.date(),
        ).select_related(
            'timetable_slot__subject',
            'timetable_slot__teacher',
            'timetable_slot__session',
        ).prefetch_related('student_attendances').distinct().order_by('-date', '-id')

        for record in all_records:
            if not record.session_held:
                status = 'cancelled'
            elif record.student_attendances.exists():
                status = 'marked'
            else:
                status = 'not_marked'
            all_attendance_rows.append({'record': record, 'status': status})

    return render(request, 'info/t_class_date.html', {
        'att_list': att_list,
        'attendance_rows': attendance_rows,
        'timetable_slot': timetable_slot,
        'all_attendance_rows': all_attendance_rows,
        'can_view_all_pdfs': can_view_all_pdfs,
        'show_only_all_marked': False,
    })


@login_required()
def cancel_class(request, att_record_id):
    att_record = get_object_or_404(AttendanceRecord, id=att_record_id)
    att_record.session_held = False
    if not att_record.remarks:
        att_record.remarks = 'Not held: College Off'
    att_record.save()
    StudentAttendance.objects.filter(attendance_record=att_record).delete()
    return HttpResponseRedirect(reverse('t_class_date', args=(att_record.timetable_slot_id,)))


@login_required()
@never_cache
def t_attendance(request, att_record_id):
    att_record = get_object_or_404(AttendanceRecord, id=att_record_id)
    timetable_slot = att_record.timetable_slot
    # Get all students enrolled in this subject
    students = Student.objects.filter(subjects=timetable_slot.subject).order_by('roll')
    
    # If no students found, try to auto-enroll students from the same course/semester/department
    if not students.exists():
        # Build filter dict
        filter_dict = {
            'course': timetable_slot.course,
            'dept': timetable_slot.department,
        }
        
        # Only filter by semester if it's set on the TimetableSlot
        if timetable_slot.semester:
            filter_dict['current_semester'] = timetable_slot.semester
        
        auto_students = Student.objects.filter(**filter_dict).exclude(status='Rejected').order_by('roll')
        
        if auto_students.exists():
            # Auto-enroll them in the subject
            for student in auto_students:
                student.subjects.add(timetable_slot.subject)
            students = auto_students
            messages.info(request, f'Auto-enrolled {students.count()} students in {timetable_slot.subject.name}')
        else:
            messages.warning(request, 'No students found for this class.')
    
    # Get existing attendance entries for these students
    existing_att = StudentAttendance.objects.filter(
        attendance_record=att_record
    ).values_list('student_id', flat=True)

    start_time_display = timetable_slot.start_time
    end_time_display = timetable_slot.end_time
    time_range_display = f"{start_time_display} - {end_time_display}".strip(' -')

    context = {
        'att_record': att_record,
        'timetable_slot': timetable_slot,
        'students': students,
        'existing_att': existing_att,
        'start_time_display': start_time_display,
        'end_time_display': end_time_display,
        'time_range_display': time_range_display,
    }
    return render(request, 'info/t_attendance.html', context)


@login_required()
@never_cache
def download_attendance_pdf(request, att_record_id):
    """Download attendance sheet PDF for a specific attendance record."""
    att_record = get_object_or_404(AttendanceRecord, id=att_record_id)
    timetable_slot = att_record.timetable_slot

    # Allow any teacher or admin to download attendance PDFs.
    if not (request.user.is_superuser or request.user.is_staff or hasattr(request.user, 'teacher')):
        return HttpResponseForbidden('You are not allowed to download this attendance report.')

    rows = StudentAttendance.objects.filter(
        attendance_record=att_record
    ).select_related('student').order_by('student__roll', 'student__name')

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    elements = []

    start_time_display = timetable_slot.start_time.strftime('%I:%M %p').lstrip('0') if timetable_slot.start_time else ''
    end_time_display = timetable_slot.end_time.strftime('%I:%M %p').lstrip('0') if timetable_slot.end_time else ''

    title = f"Attendance Report - {timetable_slot.subject.name}"
    meta = (
        f"Teacher: {timetable_slot.teacher.name}<br/>"
        f"Day: {timetable_slot.day}<br/>"
        f"Date: {att_record.date}<br/>"
        f"Time: {start_time_display} - {end_time_display}<br/>"
        f"Session: {timetable_slot.session.name}<br/>"
        f"Semester: {timetable_slot.semester}"
    )

    elements.append(Paragraph(title, styles['Title']))
    elements.append(Spacer(1, 4 * mm))
    elements.append(Paragraph(meta, styles['Normal']))
    elements.append(Spacer(1, 6 * mm))

    table_data = [['#', 'Roll', 'Student Name', 'Status']]
    present_count = 0
    absent_count = 0

    for index, row in enumerate(rows, start=1):
        status_text = 'Present' if row.status else 'Absent'
        if row.status:
            present_count += 1
        else:
            absent_count += 1
        table_data.append([
            str(index),
            str(row.student.roll or ''),
            row.student.name,
            status_text,
        ])

    if not att_record.session_held:
        reason_text = att_record.remarks or 'Not held'
        elements.append(Paragraph(f"Class was not held on this date. Reason: {reason_text}", styles['Heading4']))
    else:
        table = Table(table_data, repeatRows=1, colWidths=[15 * mm, 25 * mm, 95 * mm, 30 * mm])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f2937')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (1, -1), 'CENTER'),
            ('ALIGN', (3, 1), (3, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.lightgrey]),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ]))
        elements.append(table)
        elements.append(Spacer(1, 6 * mm))

        summary = (
            f"Total Students: {present_count + absent_count} | "
            f"Present: {present_count} | Absent: {absent_count}"
        )
        elements.append(Paragraph(summary, styles['Heading4']))

    doc.build(elements)
    pdf = buffer.getvalue()
    buffer.close()

    safe_subject = slugify(timetable_slot.subject.name) or 'subject'
    filename = f"attendance-{safe_subject}-{att_record.date}.pdf"

    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required()
def download_latest_attendance_pdf(request):
    """Redirect top menu 'Attendance PDF' link to the controlled history page."""
    if not (request.user.is_superuser or request.user.is_staff or hasattr(request.user, 'teacher')):
        return HttpResponseForbidden('Only teachers and admins can download attendance reports.')
    
    # Route to attendance history page which has proper access control and role-aware UI
    return redirect('attendance_pdf_history')


@login_required()
def attendance_pdf_history(request):
    if not (request.user.is_superuser or request.user.is_staff or hasattr(request.user, 'teacher')):
        return HttpResponseForbidden('Only teachers and admins can access attendance history.')

    records = AttendanceRecord.objects.select_related(
        'timetable_slot__teacher',
        'timetable_slot__subject',
        'timetable_slot__session',
    ).prefetch_related('student_attendances').distinct().order_by('-date', '-id')

    all_attendance_rows = []
    for record in records:
        if not record.session_held:
            status = 'cancelled'
        elif record.student_attendances.exists():
            status = 'marked'
        else:
            status = 'not_marked'
        all_attendance_rows.append({'record': record, 'status': status})

    # Reuse existing class-date template to avoid a separate history template file.
    return render(request, 'info/t_class_date.html', {
        'attendance_rows': [],
        'all_attendance_rows': all_attendance_rows,
        'can_view_all_pdfs': True,
        'show_only_all_marked': True,
    })


@login_required()
def edit_att(request, att_record_id):
    return HttpResponseForbidden('Attendance editing has been disabled.')


@login_required()
def confirm(request, att_record_id):
    att_record = get_object_or_404(AttendanceRecord, id=att_record_id)
    timetable_slot = att_record.timetable_slot
    teacher = request.user.teacher

    session_action = (request.POST.get('session_action') or 'held').strip().lower()
    non_teaching_reason = (request.POST.get('non_teaching_reason') or '').strip()
    non_teaching_note = (request.POST.get('non_teaching_note') or '').strip()

    reason_labels = {
        'college_off': 'College Off',
        'seminar_event': 'Seminar/Event',
        'other': 'Other',
    }

    if session_action == 'not_held':
        reason_label = reason_labels.get(non_teaching_reason, 'Other')
        remarks = f"Not held: {reason_label}"
        if non_teaching_note:
            remarks = f"{remarks} - {non_teaching_note}"

        att_record.session_held = False
        att_record.remarks = remarks
        att_record.marked_by = teacher
        att_record.marked_at = timezone.now()
        att_record.save(update_fields=['session_held', 'remarks', 'marked_by', 'marked_at'])

        # Ensure skipped sessions never contribute to attendance totals.
        StudentAttendance.objects.filter(attendance_record=att_record).delete()

        messages.success(request, f'Session marked as not held ({reason_label}). Attendance skipped for this lecture.')
        return HttpResponseRedirect(reverse('t_class_date', args=(timetable_slot.id,)))
    
    # Get students enrolled in this subject
    students = Student.objects.filter(subjects=timetable_slot.subject).order_by('roll')
    
    if not students.exists():
        messages.error(request, 'No students found to mark attendance for.')
        return HttpResponseRedirect(reverse('t_class_date', args=(timetable_slot.id,)))
    
    # Mark attendance for each student
    for student in students:
        status_str = request.POST.get(str(student.id), 'absent')
        status = status_str.lower() == 'present'
        
        # Create or update StudentAttendance record
        student_att, created = StudentAttendance.objects.update_or_create(
            attendance_record=att_record,
            student=student,
            defaults={'status': status}
        )
    
    # Mark attendance as held by this teacher
    att_record.marked_by = teacher
    att_record.marked_at = timezone.now()
    att_record.session_held = True
    att_record.remarks = ''
    att_record.save()
    
    messages.success(request, f'Attendance marked for {students.count()} students.')
    return HttpResponseRedirect(reverse('t_class_date', args=(timetable_slot.id,)))


@login_required()
def t_attendance_detail(request, stud_id, course_id):
    stud = get_object_or_404(Student, roll=stud_id)
    cr = get_object_or_404(Course, id=course_id)
    # Get all attendance records for this student in this course
    att_records = StudentAttendance.objects.filter(
        student=stud,
        attendance_record__session_held=True,
        attendance_record__timetable_slot__subject__course=cr
    ).order_by('-attendance_record__date')
    return render(request, 'info/t_att_detail.html', {'att_records': att_records, 'cr': cr, 'student': stud})


@login_required()
def change_att(request, student_att_id):
    return HttpResponseForbidden('Attendance editing has been disabled.')


@login_required()
def t_extra_class(request, assign_id):
    ass = get_object_or_404(Assign, id=assign_id)
    c = ass.class_id
    context = {
        'ass': ass,
        'c': c,
    }
    return render(request, 'info/t_extra_class.html', context)


@login_required()
def e_confirm(request, timetable_slot_id):
    timetable_slot = get_object_or_404(TimetableSlot, id=timetable_slot_id)
    date_str = request.POST.get('date')
    date_obj = datetime.datetime.strptime(date_str, '%Y-%m-%d').date()
    teacher = request.user.teacher
    
    # Create or get AttendanceRecord for this slot and date
    att_record, created = AttendanceRecord.objects.get_or_create(
        timetable_slot=timetable_slot,
        date=date_obj,
        defaults={'marked_by': teacher, 'session_held': True}
    )
    
    # Get students enrolled in this subject
    students = Student.objects.filter(subjects=timetable_slot.subject)
    
    for student in students:
        status_str = request.POST.get(str(student.id), 'absent')
        status = status_str.lower() == 'present'
        StudentAttendance.objects.update_or_create(
            attendance_record=att_record,
            student=student,
            defaults={'status': status}
        )
    
    att_record.marked_by = teacher
    att_record.marked_at = timezone.now()
    att_record.save()
    
    return HttpResponseRedirect(reverse('t_clas', args=(teacher.id, 1)))


def _performance_band(average_pct):
    if average_pct is None:
        return 'N/A'
    if average_pct >= 75:
        return 'Excellent'
    if average_pct >= 60:
        return 'Good'
    if average_pct >= 40:
        return 'Average'
    return 'At Risk'


def _build_teacher_report_context(teacher, session_id=None, semester=None, subject_id=None):
    teacher_subjects = Subject.objects.filter(
        teachers=teacher
    ).select_related('session', 'course', 'department').order_by('session__name', 'semester', 'name')

    sessions = Session.objects.filter(subjects__teachers=teacher).distinct().order_by('-name')
    semesters = teacher_subjects.values_list('semester', flat=True).distinct().order_by('semester')

    selected_session = None
    selected_subject = None
    semester_int = None

    if session_id:
        selected_session = Session.objects.filter(id=session_id).first()
    if semester not in (None, ''):
        try:
            semester_int = int(semester)
        except (TypeError, ValueError):
            semester_int = None

    if selected_session and semester_int and subject_id:
        selected_subject = teacher_subjects.filter(
            id=subject_id,
            session_id=selected_session.id,
            semester=semester_int,
        ).first()

    report_rows = []
    exam_type_headers = []
    summary = {}

    if selected_subject and selected_session and semester_int:
        students = Student.objects.filter(
            subjects=selected_subject,
            current_session=selected_session,
            current_semester=semester_int,
        ).exclude(status='Rejected').distinct().order_by('roll', 'name')

        conducted_exams = list(
            Exam.objects.filter(
                subject=selected_subject,
                session=selected_session,
                semester=semester_int,
                status='Conducted',
            ).order_by('exam_type')
        )
        exam_type_headers = [exam.exam_type for exam in conducted_exams]

        marks_entries = ExamMark.objects.filter(
            exam__in=conducted_exams,
            student__in=students,
        ).select_related('exam', 'student')
        marks_lookup = {
            (entry.student_id, entry.exam_id): entry
            for entry in marks_entries
        }

        attendance_records = StudentAttendance.objects.filter(
            student__in=students,
            attendance_record__session_held=True,
            attendance_record__timetable_slot__subject=selected_subject,
        ).values('student_id', 'status')

        subject_total_classes = defaultdict(int)
        subject_attended_classes = defaultdict(int)
        for item in attendance_records:
            sid = item['student_id']
            subject_total_classes[sid] += 1
            if item['status']:
                subject_attended_classes[sid] += 1

        for student in students:
            total_classes = subject_total_classes.get(student.id, 0)
            classes_attended = subject_attended_classes.get(student.id, 0)
            attendance_pct = round((classes_attended / total_classes) * 100, 2) if total_classes else None

            exam_marks = {}
            missing_marks = False
            has_absent_exam = False
            obtained_total = 0
            max_total = 0

            for exam in conducted_exams:
                mark = marks_lookup.get((student.id, exam.id))
                if not mark:
                    exam_marks[exam.exam_type] = 'N/A'
                    missing_marks = True
                    continue

                max_total += exam.total_marks
                if mark.is_absent:
                    exam_marks[exam.exam_type] = 'AB'
                    has_absent_exam = True
                else:
                    mark_value = mark.marks_obtained if mark.marks_obtained is not None else 0
                    exam_marks[exam.exam_type] = mark_value
                    obtained_total += mark_value

            average_pct = round((obtained_total / max_total) * 100, 2) if max_total > 0 else None
            low_attendance = attendance_pct is not None and attendance_pct < 75
            eligibility = attendance_pct is not None and attendance_pct >= 75 and not missing_marks

            flags = []
            if total_classes == 0:
                flags.append('Missing Attendance')
            if missing_marks and conducted_exams:
                flags.append('Missing Marks')
            if has_absent_exam:
                flags.append('Absent In Exam')
            if low_attendance and average_pct is not None and average_pct < 40:
                flags.append('Low Attendance + Low Marks')

            report_rows.append({
                'student': student,
                'total_classes': total_classes,
                'classes_attended': classes_attended,
                'attendance_pct': attendance_pct,
                'exam_marks': exam_marks,
                'obtained_total': obtained_total,
                'max_total': max_total,
                'average_pct': average_pct,
                'performance_band': _performance_band(average_pct),
                'eligibility': 'Eligible' if eligibility else 'Not Eligible',
                'flags': flags,
                'is_evaluated': max_total > 0 and not missing_marks,
                'has_absent_exam': has_absent_exam,
            })

        evaluated_rows = [r for r in report_rows if r['is_evaluated'] and r['average_pct'] is not None]
        class_average = round(sum(r['average_pct'] for r in evaluated_rows) / len(evaluated_rows), 2) if evaluated_rows else None
        topper = max(evaluated_rows, key=lambda x: x['average_pct']) if evaluated_rows else None
        lowest = min(evaluated_rows, key=lambda x: x['average_pct']) if evaluated_rows else None

        pass_count = sum(1 for r in evaluated_rows if r['average_pct'] >= 40)
        fail_count = sum(1 for r in evaluated_rows if r['average_pct'] < 40)

        summary = {
            'class_strength': len(report_rows),
            'students_evaluated': len(evaluated_rows),
            'class_average': class_average,
            'topper': topper,
            'lowest': lowest,
            'pass_count': pass_count,
            'fail_count': fail_count,
            'absent_count': sum(1 for r in report_rows if r['has_absent_exam']),
            'missing_attendance_count': sum(1 for r in report_rows if 'Missing Attendance' in r['flags']),
            'missing_marks_count': sum(1 for r in report_rows if 'Missing Marks' in r['flags']),
            'eligible_count': sum(1 for r in report_rows if r['eligibility'] == 'Eligible'),
        }

    return {
        'teacher_subjects': teacher_subjects,
        'sessions': sessions,
        'semesters': semesters,
        'selected_session': str(session_id) if session_id else '',
        'selected_semester': str(semester) if semester not in (None, '') else '',
        'selected_subject': str(subject_id) if subject_id else '',
        'selected_subject_obj': selected_subject,
        'selected_session_obj': selected_session,
        'selected_semester_int': semester_int,
        'report_rows': report_rows,
        'exam_type_headers': exam_type_headers,
        'summary': summary,
    }


@login_required
def teacher_reports(request):
    if not hasattr(request.user, 'teacher'):
        messages.error(request, 'Only teachers can access reports.')
        return redirect('home')

    teacher = request.user.teacher
    session_id = request.GET.get('session')
    semester = request.GET.get('semester')
    subject_id = request.GET.get('subject')

    context = _build_teacher_report_context(teacher, session_id, semester, subject_id)
    return render(request, 'info/teacher_reports.html', context)


@login_required
def teacher_reports_pdf(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can download reports.')

    teacher = request.user.teacher
    session_id = request.GET.get('session')
    semester = request.GET.get('semester')
    subject_id = request.GET.get('subject')

    context = _build_teacher_report_context(teacher, session_id, semester, subject_id)
    if not context['selected_subject_obj']:
        messages.error(request, 'Select valid session, semester, and subject before downloading the report.')
        return redirect('teacher_reports')

    if not context['report_rows']:
        messages.error(request, 'No student data found for the selected filters.')
        return redirect('teacher_reports')

    subject = context['selected_subject_obj']
    session_obj = context['selected_session_obj']
    semester_int = context['selected_semester_int']

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=10 * mm,
        rightMargin=10 * mm,
        topMargin=10 * mm,
        bottomMargin=10 * mm,
    )
    styles = getSampleStyleSheet()
    elements = []

    generated_at = timezone.localtime().strftime('%Y-%m-%d %I:%M %p')
    title = 'Internal Academic Report'
    meta = (
        f"Teacher: {teacher.name}<br/>"
        f"Subject: {subject.name}<br/>"
        f"Session: {session_obj.name}<br/>"
        f"Semester: {semester_int}<br/>"
        f"Generated At: {generated_at}"
    )

    elements.append(Paragraph(title, styles['Title']))
    elements.append(Spacer(1, 2 * mm))
    elements.append(Paragraph(meta, styles['Normal']))
    elements.append(Spacer(1, 4 * mm))

    summary = context['summary']
    summary_lines = [
        f"Class Strength: {summary.get('class_strength', 0)}",
        f"Students Evaluated: {summary.get('students_evaluated', 0)}",
        f"Class Average: {summary.get('class_average', 'N/A')}",
        f"Pass: {summary.get('pass_count', 0)}",
        f"Fail: {summary.get('fail_count', 0)}",
        f"Eligible: {summary.get('eligible_count', 0)}",
    ]
    elements.append(Paragraph(' | '.join(summary_lines), styles['Heading4']))
    elements.append(Spacer(1, 3 * mm))

    headers = ['Roll', 'Student', 'Attendance %']
    headers.extend(context['exam_type_headers'])
    headers.extend(['Total', 'Avg %', 'Band', 'Eligibility', 'Flags'])

    table_data = [headers]
    for row in context['report_rows']:
        table_row = [
            str(row['student'].roll or '-'),
            row['student'].name,
            f"{row['attendance_pct']}" if row['attendance_pct'] is not None else 'N/A',
        ]
        for exam_type in context['exam_type_headers']:
            table_row.append(str(row['exam_marks'].get(exam_type, 'N/A')))
        total_text = f"{row['obtained_total']}/{row['max_total']}" if row['max_total'] else 'N/A'
        table_row.extend([
            total_text,
            f"{row['average_pct']}" if row['average_pct'] is not None else 'N/A',
            row['performance_band'],
            row['eligibility'],
            ', '.join(row['flags']) if row['flags'] else '-',
        ])
        table_data.append(table_row)

    col_count = len(headers)
    col_width = 190 * mm / col_count
    table = Table(table_data, repeatRows=1, colWidths=[col_width] * col_count)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f2937')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 7),
        ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.grey),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.lightgrey]),
        ('TOPPADDING', (0, 0), (-1, -1), 3),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
    ]))
    elements.append(table)

    doc.build(elements)
    pdf = buffer.getvalue()
    buffer.close()

    safe_subject = slugify(subject.name) or 'subject'
    filename = f"report-{safe_subject}-{session_obj.name}-sem-{semester_int}.pdf"

    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
def teacher_student_report_pdf(request, student_id):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can download student reports.')

    teacher = request.user.teacher
    session_id = request.GET.get('session')
    semester = request.GET.get('semester')
    subject_id = request.GET.get('subject')

    context = _build_teacher_report_context(teacher, session_id, semester, subject_id)
    if not context['selected_subject_obj']:
        messages.error(request, 'Select valid session, semester, and subject before downloading student report.')
        return redirect('teacher_reports')

    row = next((r for r in context['report_rows'] if r['student'].id == student_id), None)
    if not row:
        messages.error(request, 'Student not found in selected report filters.')
        return redirect('teacher_reports')

    student = row['student']
    subject = context['selected_subject_obj']
    session_obj = context['selected_session_obj']
    semester_int = context['selected_semester_int']

    subject_assignments_qs = SubjectAssignment.objects.filter(
        teacher=teacher,
        subject=subject,
    ).order_by('-created_at')
    subject_assignment_ids = list(subject_assignments_qs.values_list('id', flat=True))
    submitted_assignment_ids = set(
        AssignmentSubmission.objects.filter(
            student=student,
            assignment_id__in=subject_assignment_ids,
        ).values_list('assignment_id', flat=True)
    )
    subject_assignment_total = len(subject_assignment_ids)
    subject_submitted_total = len(submitted_assignment_ids)
    subject_pending_total = max(subject_assignment_total - subject_submitted_total, 0)
    subject_submission_pct = round((subject_submitted_total / subject_assignment_total) * 100, 2) if subject_assignment_total else None

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'StudentReportTitle',
        parent=styles['Title'],
        fontName='Helvetica-Bold',
        fontSize=20,
        textColor=colors.HexColor('#0f172a'),
        alignment=TA_CENTER,
        spaceAfter=8,
    )
    section_style = ParagraphStyle(
        'StudentReportSection',
        parent=styles['Heading4'],
        fontName='Helvetica-Bold',
        fontSize=11,
        textColor=colors.HexColor('#1d4ed8'),
        spaceBefore=4,
        spaceAfter=4,
    )
    normal_small = ParagraphStyle(
        'StudentReportNormalSmall',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#111827'),
    )
    elements = []

    generated_at = timezone.localtime().strftime('%Y-%m-%d %I:%M %p')
    title = f"Student Academic Report"
    subtitle = f"{student.name} | {subject.name} | {session_obj.name} | Semester {semester_int}"
    elements.append(Paragraph(title, title_style))
    elements.append(Paragraph(subtitle, ParagraphStyle(
        'StudentReportSubtitle',
        parent=normal_small,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#4b5563'),
        spaceAfter=8,
    )))

    photo_cell = Paragraph('No Photo', normal_small)
    if student.photo and hasattr(student.photo, 'path'):
        try:
            photo_cell = Image(student.photo.path, width=30 * mm, height=34 * mm)
        except Exception:
            pass

    profile_data = [
        [Paragraph('<b>Student Name</b>', normal_small), Paragraph(student.name, normal_small)],
        [Paragraph('<b>Roll</b>', normal_small), Paragraph(str(student.roll or '-'), normal_small)],
        [Paragraph('<b>Department</b>', normal_small), Paragraph(str(student.dept.name if student.dept else '-'), normal_small)],
        [Paragraph('<b>Course</b>', normal_small), Paragraph(str(student.course.name if student.course else '-'), normal_small)],
        [Paragraph('<b>Subject</b>', normal_small), Paragraph(subject.name, normal_small)],
        [Paragraph('<b>Session</b>', normal_small), Paragraph(session_obj.name, normal_small)],
        [Paragraph('<b>Semester</b>', normal_small), Paragraph(str(semester_int), normal_small)],
        [Paragraph('<b>Generated At</b>', normal_small), Paragraph(generated_at, normal_small)],
    ]
    profile_table = Table(profile_data, colWidths=[42 * mm, 106 * mm])
    profile_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#eff6ff')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#1e3a8a')),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#cbd5e1')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))

    head_table = Table([[photo_cell, profile_table]], colWidths=[33 * mm, 153 * mm])
    head_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, 0), 'CENTER'),
        ('BOX', (0, 0), (-1, -1), 0.6, colors.HexColor('#cbd5e1')),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(head_table)
    elements.append(Spacer(1, 4 * mm))

    elements.append(Paragraph('Exam-wise Marks', section_style))
    exam_headers = ['Exam Type', 'Marks']
    exam_data = [exam_headers]
    for exam_type in context['exam_type_headers']:
        exam_data.append([exam_type, str(row['exam_marks'].get(exam_type, 'N/A'))])

    exam_table = Table(exam_data, colWidths=[120 * mm, 66 * mm])
    exam_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0f766e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('ALIGN', (1, 1), (1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#9ca3af')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f9fafb'), colors.HexColor('#f3f4f6')]),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]))
    elements.append(exam_table)
    elements.append(Spacer(1, 4 * mm))

    attendance_text = (
        f"Attendance: {row['attendance_pct']}%"
        if row['attendance_pct'] is not None
        else 'Attendance: N/A'
    )
    attendance_text += f" ({row['classes_attended']}/{row['total_classes']} classes)"

    total_text = f"{row['obtained_total']}/{row['max_total']}" if row['max_total'] else 'N/A'
    elements.append(Paragraph('Performance Snapshot', section_style))
    metrics_data = [
        ['Attendance', attendance_text],
        ['Total Marks', total_text],
        ['Average %', f"{row['average_pct'] if row['average_pct'] is not None else 'N/A'}"],
        ['Performance Band', row['performance_band']],
        ['Eligibility', row['eligibility']],
        ['Flags', ', '.join(row['flags']) if row['flags'] else '-'],
    ]
    metrics_table = Table(metrics_data, colWidths=[48 * mm, 138 * mm])
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#fef3c7')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#92400e')),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#d1d5db')),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    elements.append(metrics_table)

    elements.append(Spacer(1, 4 * mm))
    elements.append(Paragraph('Assignment Submission Summary', section_style))
    assignment_data = [
        ['Subject', 'Total Assignments', 'Submitted', 'Pending', 'Submission %'],
        [
            subject.name,
            str(subject_assignment_total),
            str(subject_submitted_total),
            str(subject_pending_total),
            f"{subject_submission_pct if subject_submission_pct is not None else 'N/A'}",
        ],
    ]
    assignment_table = Table(assignment_data, colWidths=[74 * mm, 30 * mm, 24 * mm, 24 * mm, 34 * mm])
    assignment_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#334155')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#9ca3af')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f8fafc')]),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
    ]))
    elements.append(assignment_table)

    def draw_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#6b7280'))
        canvas.drawString(12 * mm, 7 * mm, f"Generated by {teacher.name} on {generated_at}")
        canvas.drawRightString(198 * mm, 7 * mm, f"Page {canvas.getPageNumber()}")

        canvas.setFont('Helvetica-Bold', 34)
        canvas.setFillColor(colors.HexColor('#e5e7eb'))
        canvas.saveState()
        canvas.translate(105 * mm, 145 * mm)
        canvas.rotate(35)
        canvas.drawCentredString(0, 0, 'INTERNAL ACADEMIC REPORT')
        canvas.restoreState()
        canvas.restoreState()

    doc.build(elements, onFirstPage=draw_footer, onLaterPages=draw_footer)
    pdf = buffer.getvalue()
    buffer.close()

    safe_student = slugify(student.name) or 'student'
    safe_subject = slugify(subject.name) or 'subject'
    filename = f"student-report-{safe_student}-{safe_subject}-sem-{semester_int}.pdf"

    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required()
def t_report(request, assign_id):
    # Legacy route retained for backward compatibility.
    return redirect('teacher_reports')


@login_required()
def timetable(request, class_id):
    if not hasattr(request.user, 'student'):
        messages.error(request, 'Only students can view this timetable.')
        return redirect('home')

    student = request.user.student
    session_obj = get_object_or_404(Session, id=class_id)

    slots = TimetableSlot.objects.filter(
        session=session_obj,
        semester=student.current_semester,
        department=student.dept,
        course=student.course,
    ).select_related('subject', 'teacher').order_by('day', 'start_time')

    time_slot_rows = sorted(
        slots.values_list('start_time', 'end_time').distinct(),
        key=lambda value: value[0]
    )

    day_names = [day for day, _ in DAYS_OF_WEEK]
    slot_map = defaultdict(list)
    for slot in slots:
        slot_map[(slot.day, slot.start_time)].append({
            'text': f'{slot.subject.name} ({slot.teacher.name})',
            'free_teacher_url': reverse('free_teachers', args=[slot.id]),
        })

    matrix = []
    for day in day_names:
        row = [day]
        for start_time, _end_time in time_slot_rows:
            row.append(slot_map.get((day, start_time), []))
        matrix.append(row)

    if not slots.exists():
        messages.info(request, 'No timetable has been assigned for your current session and semester yet.')

    context = {
        'matrix': matrix,
        'time_slots': time_slot_rows,
        'student': student,
        'session_obj': session_obj,
        'semester': student.current_semester,
    }
    return render(request, 'info/timetable.html', context)




@login_required()
def free_teachers(request, asst_id):
    # Primary flow: modern timetable slots.
    timetable_slot = TimetableSlot.objects.filter(id=asst_id).select_related(
        'teacher', 'session', 'department', 'course', 'subject'
    ).first()

    if timetable_slot:
        teachers_qs = Teacher.objects.filter(status='Approved')
        if timetable_slot.department_id:
            teachers_qs = teachers_qs.filter(dept_id=timetable_slot.department_id)
        if timetable_slot.course_id:
            teachers_qs = teachers_qs.filter(course_id=timetable_slot.course_id)

        # Fallbacks for data sets where status/department/course mappings are incomplete.
        if not teachers_qs.exists() and timetable_slot.department_id:
            teachers_qs = Teacher.objects.filter(dept_id=timetable_slot.department_id)

        if not teachers_qs.exists() and timetable_slot.course_id:
            teachers_qs = Teacher.objects.filter(course_id=timetable_slot.course_id)

        if not teachers_qs.exists():
            teachers_qs = Teacher.objects.all()

        teachers = teachers_qs.distinct().order_by('name')

        overlapping_slots = TimetableSlot.objects.filter(
            day=timetable_slot.day,
            session=timetable_slot.session,
            semester=timetable_slot.semester,
            start_time=timetable_slot.start_time,
            end_time=timetable_slot.end_time,
            teacher__in=teachers,
        ).select_related('subject', 'teacher')
        busy_by_teacher_id = {slot.teacher_id: slot for slot in overlapping_slots}

        teacher_statuses = []
        for teacher in teachers:
            busy_slot = busy_by_teacher_id.get(teacher.id)
            if busy_slot is None:
                teacher_statuses.append({
                    'teacher': teacher,
                    'is_free': True,
                    'busy_subject': '',
                })

        ft_list = [item['teacher'] for item in teacher_statuses]
        start_display = timetable_slot.start_time.strftime('%I:%M %p').lstrip('0') if timetable_slot.start_time else '-'
        end_display = timetable_slot.end_time.strftime('%I:%M %p').lstrip('0') if timetable_slot.end_time else '-'
        session_name = ''
        if timetable_slot.session:
            session_name = (timetable_slot.session.name or '').strip()
        if not session_name:
            session_name = f"Session {timetable_slot.session_id}" if timetable_slot.session_id else '-'
        context = {
            'ft_list': ft_list,
            'teacher_statuses': teacher_statuses,
            'slot_context': timetable_slot,
            'source': 'timetable_slot',
            'slot_day': timetable_slot.day,
            'slot_start_display': start_display,
            'slot_end_display': end_display,
            'slot_session_name': session_name,
            'slot_semester': timetable_slot.semester,
        }
        return render(request, 'info/free_teachers.html', context)

    # Legacy fallback flow: AssignTime based records.
    asst = get_object_or_404(AssignTime, id=asst_id)
    ft_list = []
    teacher_statuses = []
    teacher_list = Teacher.objects.filter(assign__session_id=asst.assign.session_id).distinct()
    for teacher in teacher_list:
        at_list = AssignTime.objects.filter(assign__teacher=teacher)
        is_free = not any(at.period == asst.period and at.day == asst.day for at in at_list)
        if is_free:
            teacher_statuses.append({
                'teacher': teacher,
                'is_free': True,
                'busy_subject': '',
            })
            ft_list.append(teacher)

    context = {
        'ft_list': ft_list,
        'teacher_statuses': teacher_statuses,
        'slot_context': asst,
        'source': 'legacy_assign_time',
        'slot_day': asst.day,
        'slot_start_display': asst.period,
        'slot_end_display': '',
        'slot_session_name': (asst.assign.session.name.strip() if asst.assign and asst.assign.session and asst.assign.session.name else (f"Session {asst.assign.session_id}" if asst.assign and asst.assign.session_id else '-')),
        'slot_semester': '',
    }
    return render(request, 'info/free_teachers.html', context)

@login_required
@never_cache
def edit_timetable(request, teacher_id):
    if not hasattr(request.user, 'teacher'):
        messages.error(request, 'Only teachers can edit timetable.')
        return redirect('home')
    teacher = request.user.teacher
    if teacher_id != teacher.id:
        return redirect('edit_timetable', teacher_id=teacher.id)
    department = teacher.dept
    session_list = Session.objects.all()

    session_id = request.GET.get('session') or request.POST.get('session')
    semester_str = request.GET.get('semester') or request.POST.get('semester')

    session = None
    semester = None

    if session_id:
        session = get_object_or_404(Session, id=session_id)
    if semester_str:
        try:
            semester = int(semester_str)
        except ValueError:
            semester = None

    # Teachers can create slots only for subjects assigned to them.
    assigned_subjects_qs = Subject.objects.filter(
        department=department,
        teachers=teacher
    ).select_related('session').distinct().order_by('name')

    if request.method == 'POST':
        day = request.POST.get('day')
        start_time_str = request.POST.get('start_time')
        end_time_str = request.POST.get('end_time')
        subject_id = request.POST.get('subject')
        post_semester_str = request.POST.get('semester')

        if day and start_time_str and end_time_str and subject_id and session_id and post_semester_str:
            start_time = datetime.datetime.strptime(start_time_str, "%H:%M").time()
            end_time = datetime.datetime.strptime(end_time_str, "%H:%M").time()
            subject = assigned_subjects_qs.filter(pk=subject_id).first()
            if not subject:
                messages.error(request, 'You can only create slots for subjects assigned to you.')
                redirect_url = reverse('edit_timetable', args=[teacher.id])
                if session_id and post_semester_str:
                    redirect_url = f"{redirect_url}?session={session_id}&semester={post_semester_str}"
                return redirect(redirect_url)

            session_obj = get_object_or_404(Session, id=session_id)
            user_semester = int(post_semester_str)  

            TimetableSlot.objects.update_or_create(
                teacher=teacher,
                day=day,
                start_time=start_time,
                session=session_obj,
                defaults={
                    'end_time': end_time,
                    'subject': subject,
                    'course': teacher.course,
                    'department': department,
                    'semester': user_semester
                }
            )

            redirect_url = reverse('edit_timetable', args=[teacher.id])
            redirect_url = f"{redirect_url}?session={session_obj.id}&semester={user_semester}"
            return redirect(redirect_url)

    # Filter assigned subjects by optional session and semester.
    all_subjects_qs = assigned_subjects_qs
    subjects = all_subjects_qs
    if session:
        subjects = subjects.filter(session=session)
    if semester is not None:
        subjects = subjects.filter(semester=semester)

    all_subjects_data = [
        {
            'id': subject.id,
            'name': subject.name,
            'session_id': subject.session_id,
            'semester': subject.semester,
        }
        for subject in all_subjects_qs
    ]

    assignments = TimetableSlot.objects.filter(
        teacher=teacher,
        teacher__user=request.user
    ).order_by('day', 'start_time')

    context = {
        'teacher': teacher,
        'department': department,
        'subjects': subjects,
        'session_list': session_list,
        'assignments': assignments,
        'semester_list': ['1', '2', '3', '4', '5', '6', '7', '8'],
        'days_of_week': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'],
        'selected_session': getattr(session, 'id', ''),
        'selected_semester': semester_str or '',
        'all_subjects_json': json.dumps(all_subjects_data),
    }
    return render(request, 'info/t_timetable.html', context)

@login_required
@never_cache
def teacher_timetable(request, teacher_id):
    if not hasattr(request.user, 'teacher'):
        messages.error(request, 'Only teachers can view timetable.')
        return redirect('home')
    teacher = request.user.teacher
    if teacher_id != teacher.id:
        return redirect('teacher_timetable', teacher_id=teacher.id)
    
    # Get filters from POST (modal selection)
    session_id = request.POST.get("session")
    semester_str = request.POST.get("semester")
    
    session = None
    semester = None
    if session_id:
        session = get_object_or_404(Session, id=session_id)
    if semester_str:
        semester = int(semester_str)

    # Build empty matrix if no filters
    time_slots = []
    matrix = []
    
    if session and semester:
        # Shared timetable view: show all filled slots in teacher's department
        slots = TimetableSlot.objects.filter(
            department=teacher.dept,
            session=session,
            semester=semester
        ).select_related('subject', 'teacher')

        # Get unique time slots from actual saved data
        time_slots = sorted(
            slots.values_list('start_time', 'end_time').distinct(),
            key=lambda x: x[0]
        )
        
        DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        
        # Map (day, start_time) -> list of "Subject (Teacher)"
        slot_map = defaultdict(list)
        for slot in slots:
            key = (slot.day, slot.start_time)
            slot_map[key].append(f"{slot.subject.name} ({slot.teacher.name})")
        
        # Build matrix
        matrix = []
        for day in DAYS:
            row = [day]
            for start_time, end_time in time_slots:
                # Fixed lunch detection
                start_hour = start_time.hour
                if 13 <= start_hour < 14:  # 1-2 PM lunch
                    row.append("LUNCH")
                else:
                    entries = slot_map.get((day, start_time), [])
                    row.append(' | '.join(entries) if entries else "")
            matrix.append(row)

    context = {
        'teacher': teacher,
        'matrix': matrix,
        'time_slots': time_slots,
        'session_list': Session.objects.all(),
        'semester_list': ['1', '2', '3', '4', '5', '6', '7', '8'],
        'selected_session': session.id if session else "",
        'selected_semester': semester_str or "",
        'show_modal': not (session_id and semester_str),
    }
    return render(request, "info/teacher_timetable.html", context)



@login_required
def delete_timetable_slot(request, slot_id):
    slot = get_object_or_404(TimetableSlot, id=slot_id)
    if not hasattr(request.user, 'teacher') or request.user.teacher != slot.teacher:
        messages.error(request, 'You can only delete your own timetable slots.')
        return redirect('home')
    teacher_id = slot.teacher.id
    if request.method == 'POST':
        slot.delete()
        messages.success(request, 'Timetable slot deleted successfully!')
    return redirect('edit_timetable', teacher_id=teacher_id)


@login_required()
def student_subjects(request):
    if not hasattr(request.user, 'student'):
        messages.error(request, 'Only students can view this page.')
        return redirect('home')

    student = request.user.student
    subjects = student.subjects.select_related('session', 'department', 'course').order_by(
        '-session__name',
        'semester',
        'name',
    )

    context = {
        'student': student,
        'subjects': subjects,
    }
    return render(request, 'info/student_subjects.html', context)


@login_required()
def marks_list(request, stud_id):
    if not hasattr(request.user, 'student'):
        messages.error(request, 'Only students can view this page.')
        return redirect('home')

    student = request.user.student

    # Student can only view their own marks; normalize URL back to their roll.
    allowed_ids = {str(student.id)}
    if student.roll is not None:
        allowed_ids.add(str(student.roll))
    if str(stud_id) not in allowed_ids:
        canonical_id = student.roll if student.roll is not None else student.id
        return redirect('marks_list', stud_id=canonical_id)

    exam_type_headers = [label for label, _ in Exam.EXAM_TYPE_CHOICES]
    selected_session = request.GET.get('session', '')
    selected_semester = request.GET.get('semester', '')
    selected_exam_type = request.GET.get('exam_type', '')
    filters_applied = bool(selected_session and selected_semester)

    base_exams = Exam.objects.filter(
        Q(scheduled_students=student) | Q(marks_entries__student=student),
        status='Conducted',
    ).select_related('subject', 'session').distinct()

    sessions = base_exams.values_list('session_id', 'session__name').distinct().order_by('-session__name')
    semesters = base_exams.values_list('semester', flat=True).distinct().order_by('semester')

    conducted_exams = base_exams.none()
    if filters_applied:
        conducted_exams = base_exams.filter(
            session_id=selected_session,
            semester=selected_semester,
        )
        if selected_exam_type:
            conducted_exams = conducted_exams.filter(exam_type=selected_exam_type)
        conducted_exams = conducted_exams.order_by('-session__name', 'semester', 'subject__name', 'exam_type')

    marks_lookup = {
        mark.exam_id: mark
        for mark in ExamMark.objects.filter(
            exam__in=conducted_exams,
            student=student,
        ).select_related('exam')
    }

    grouped_rows = {}
    for exam in conducted_exams:
        key = (exam.subject_id, exam.session_id, exam.semester)
        if key not in grouped_rows:
            grouped_rows[key] = {
                'subject_name': exam.subject.name,
                'session_name': exam.session.name if exam.session and exam.session.name else '-',
                'semester': exam.semester,
                'exam_marks': {exam_type: '-' for exam_type in exam_type_headers},
            }

        mark_entry = marks_lookup.get(exam.id)
        if not mark_entry:
            cell_value = '-'
        elif mark_entry.is_absent:
            cell_value = 'AB'
        elif mark_entry.marks_obtained is None:
            cell_value = '-'
        else:
            cell_value = f"{mark_entry.marks_obtained}/{exam.total_marks}"

        grouped_rows[key]['exam_marks'][exam.exam_type] = cell_value

    marks_rows = []
    for row in grouped_rows.values():
        row['marks_cells'] = [row['exam_marks'][exam_type] for exam_type in exam_type_headers]
        marks_rows.append(row)

    marks_entries = list(marks_lookup.values())
    summary = {
        'subjects_count': len(marks_rows),
        'exams_count': conducted_exams.count(),
        'entered_count': sum(1 for m in marks_entries if m.marks_obtained is not None and not m.is_absent),
        'absent_count': sum(1 for m in marks_entries if m.is_absent),
    }

    context = {
        'student': student,
        'exam_type_headers': exam_type_headers,
        'marks_rows': marks_rows,
        'sessions': sessions,
        'semesters': semesters,
        'exam_types': Exam.EXAM_TYPE_CHOICES,
        'selected_session': selected_session,
        'selected_semester': selected_semester,
        'selected_exam_type': selected_exam_type,
        'filters_applied': filters_applied,
        'summary': summary,
    }
    return render(request, 'info/marks_list.html', context)


@login_required()
def student_exam_schedule(request):
    if not hasattr(request.user, 'student'):
        messages.error(request, 'Only students can view this page.')
        return redirect('home')

    student = request.user.student
    selected_session = request.GET.get('session', '')
    selected_semester = request.GET.get('semester', '')
    selected_exam_type = request.GET.get('exam_type', '')
    selected_status = request.GET.get('status', '')
    filters_applied = bool(
        selected_session or selected_semester or selected_exam_type or selected_status
    )

    base_exams = Exam.objects.filter(
        scheduled_students=student,
    ).select_related('subject', 'session').distinct()

    exams_qs = base_exams.none()
    if filters_applied:
        exams_qs = base_exams
        if selected_session:
            exams_qs = exams_qs.filter(session_id=selected_session)
        if selected_semester:
            exams_qs = exams_qs.filter(semester=selected_semester)
        if selected_exam_type:
            exams_qs = exams_qs.filter(exam_type=selected_exam_type)
        if selected_status:
            exams_qs = exams_qs.filter(status=selected_status)

    exams_qs = exams_qs.order_by('date_conducted', 'exam_time', '-created_at')
    exam_rows = [{'exam': exam} for exam in exams_qs]

    summary = {
        'total': len(exam_rows),
        'pending': sum(1 for row in exam_rows if row['exam'].status == 'Pending'),
        'conducted': sum(1 for row in exam_rows if row['exam'].status == 'Conducted'),
    }

    context = {
        'student': student,
        'exam_rows': exam_rows,
        'sessions': base_exams.values_list('session_id', 'session__name').distinct().order_by('-session__name'),
        'semesters': base_exams.values_list('semester', flat=True).distinct().order_by('semester'),
        'exam_types': Exam.EXAM_TYPE_CHOICES,
        'statuses': Exam.STATUS_CHOICES,
        'selected_session': selected_session,
        'selected_semester': selected_semester,
        'selected_exam_type': selected_exam_type,
        'selected_status': selected_status,
        'filters_applied': filters_applied,
        'summary': summary,
    }
    return render(request, 'info/student_exam_schedule.html', context)


@login_required()
def student_marks(request, assign_id):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can view class marks.')
    teacher = request.user.teacher
    ass = get_object_or_404(
        Assign.objects.select_related('session', 'course', 'teacher'),
        id=assign_id, teacher=teacher,
    )
    students = Student.objects.filter(
        dept=teacher.dept, course=ass.course, current_session=ass.session,
        status__iexact='Approved', academic_status=Student.ACADEMIC_STATUS_ENROLLED,
    )
    sc_list = StudentCourse.objects.filter(student__in=students, course=ass.course).select_related('student')
    return render(request, 'info/t_student_marks.html', {'sc_list': sc_list, 'assignment': ass})


@login_required
@user_passes_test(is_superuser)
def add_teacher(request):
    messages.info(request, 'Teacher accounts are created through the registration request and approval workflow.')
    return redirect('register_teacher')


@login_required
@user_passes_test(is_superuser)
def add_student(request):
    messages.info(request, 'Student accounts are created through the admission request and approval workflow.')
    return redirect('register_student')


def generate_sessions_for_admission(admission_year, duration):
    if not admission_year or not duration or int(duration) < 1:
        return []
    sessions = []
    for i in range(int(duration)):
        start_year = admission_year + i
        end_year = start_year + 1
        name = f"{start_year}-{end_year}"
        session = get_or_create_canonical_session(name)
        if session:
            sessions.append(session)
    return sessions


@login_required
@never_cache
def manage_academic_progression(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("You are not authorized to manage academic progression.")
    if request.method != 'POST':
        return redirect('pending_requests_list')

    action = request.POST.get('action')
    selected_session_id = request.POST.get('session_id')
    filter_admission_year = request.POST.get('filter_admission_year')
    filter_course_id = request.POST.get('filter_course_id')
    filter_current_session_id = request.POST.get('filter_current_session_id')

    def build_student_queryset():
        qs = Student.objects.filter(status__iexact='Approved', academic_status=Student.ACADEMIC_STATUS_ENROLLED).select_related(
            'course', 'dept', 'current_session'
        ).order_by('admission_year', 'roll', 'id')
        if filter_admission_year:
            try:
                qs = qs.filter(admission_year=int(filter_admission_year))
            except (TypeError, ValueError):
                messages.error(request, 'Invalid admission year filter.')
                return Student.objects.none()
        if filter_course_id:
            try:
                qs = qs.filter(course_id=int(filter_course_id))
            except (TypeError, ValueError):
                messages.error(request, 'Invalid course filter.')
                return Student.objects.none()
        if filter_current_session_id:
            try:
                qs = qs.filter(current_session_id=int(filter_current_session_id))
            except (TypeError, ValueError):
                messages.error(request, 'Invalid current session filter.')
                return Student.objects.none()
        return qs

    if action == 'set_current_session':
        selected = get_object_or_404(Session, id=selected_session_id)
        selected.is_current = True
        selected.save()
        messages.success(request, f'College active academic session is now {selected.name}. Student cohort sessions are managed separately.')
        return redirect('pending_requests_list')

    if action == 'promote_semester':
        students = build_student_queryset()
        promoted_count = 0
        skipped_count = 0
        missing_admission_year = 0
        for student in students.iterator():
            if not student.admission_year:
                missing_admission_year += 1
                skipped_count += 1
                continue
            if student.promote_semester(actor=request.user):
                promoted_count += 1
            else:
                skipped_count += 1
        messages.success(
            request,
            f'Semester advancement finished. Promoted: {promoted_count}; skipped/already processed or at course end: {skipped_count}; missing admission year: {missing_admission_year}.',
        )
        return redirect('pending_requests_list')

    if action == 'graduate_completed':
        students = build_student_queryset()
        active_session = Session.objects.filter(is_current=True).first()
        active_year = get_session_start_year(active_session) if active_session else None
        graduated_count = 0
        skipped_count = 0
        for student in students.iterator():
            maximum = student.max_semesters()
            final_session = student.session_for_semester(maximum) if maximum else None
            final_year = get_session_start_year(final_session) if final_session else None
            if (
                maximum
                and student.current_semester == maximum
                and final_session
                and active_year is not None
                and final_year is not None
                and student.current_session_id == final_session.pk
                and active_year >= final_year
            ):
                student.academic_status = Student.ACADEMIC_STATUS_GRADUATED
                student.graduated_at = timezone.localdate()
                student.save(update_fields=['academic_status', 'graduated_at'])
                graduated_count += 1
            else:
                skipped_count += 1
        messages.success(
            request,
            f'Graduation processing finished. Graduated: {graduated_count}; skipped because the student is not in the final semester, the final session has not started, or course duration is missing: {skipped_count}.',
        )
        return redirect('pending_requests_list')

    if action == 'promote_session':
        # This is a repair/synchronisation operation only. It must never
        # overwrite every student's batch session with the college-wide flag.
        students = build_student_queryset()
        updated_count = 0
        skipped_count = 0
        for student in students.iterator():
            if student.promote_session():
                updated_count += 1
            else:
                skipped_count += 1
        messages.success(
            request,
            f'Cohort session synchronisation finished. Updated: {updated_count}; skipped (missing admission year/session): {skipped_count}. No semester numbers were changed.',
        )
        return redirect('pending_requests_list')

    if action == 'apply_session_term':
        if not filter_course_id:
            messages.error(request, 'Select a course before applying a semester.')
            return redirect('pending_requests_list')
        try:
            target_semester = int(request.POST.get('target_semester') or '')
        except (TypeError, ValueError):
            messages.error(request, 'Select a valid semester.')
            return redirect('pending_requests_list')

        updated_count = 0
        skipped_count = 0
        for student in build_student_queryset().iterator():
            if not student.admission_year:
                skipped_count += 1
                continue
            if student.set_academic_position(target_semester, actor=request.user, allow_same=True):
                updated_count += 1
            else:
                skipped_count += 1

        messages.success(
            request,
            f'Applied Semester {target_semester} to the selected cohort(s). Updated/synchronised: {updated_count}; skipped (invalid, backward or already processed transition): {skipped_count}. Each student was assigned the session derived from their own admission year.',
        )
        return redirect('pending_requests_list')

    messages.error(request, 'Invalid academic progression action.')
    return redirect('pending_requests_list')


@login_required
@never_cache
def student_fees(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can access fee invoices.')
    student = request.user.student
    if (student.status or '').strip().lower() != 'approved' or student.academic_status != Student.ACADEMIC_STATUS_ENROLLED:
        return HttpResponseForbidden('Your student account is not approved yet.')

    invoices = list(
        StudentFeeInvoice.objects.filter(student=student)
        .select_related('session')
        .prefetch_related('items', 'payments')
        .order_by('-issue_date', '-id')
    )
    for invoice in invoices:
        paid = sum(
            (payment.amount for payment in invoice.payments.all() if payment.status == FeePayment.STATUS_SUCCESSFUL),
            Decimal('0.00'),
        )
        invoice.amount_paid_display = paid
        invoice.amount_due_display = max(Decimal('0.00'), invoice.total_amount - paid)
        if invoice.status != StudentFeeInvoice.STATUS_CANCELLED:
            invoice.status_display = (
                StudentFeeInvoice.STATUS_PAID if paid >= invoice.total_amount and invoice.total_amount > 0
                else StudentFeeInvoice.STATUS_PARTIAL if paid > 0
                else StudentFeeInvoice.STATUS_PENDING
            )
        else:
            invoice.status_display = StudentFeeInvoice.STATUS_CANCELLED

    return render(request, 'info/student_fees.html', {
        'student': student,
        'invoices': invoices,
        'razorpay_configured': bool(settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET),
        'razorpay_key_id': settings.RAZORPAY_KEY_ID,
        'currency': settings.RAZORPAY_CURRENCY,
    })


def _razorpay_request(method, endpoint, payload=None):
    if not settings.RAZORPAY_KEY_ID or not settings.RAZORPAY_KEY_SECRET:
        raise RuntimeError('Online fee payments are not configured. The college must add Razorpay API credentials.')
    credentials = base64.b64encode(
        f'{settings.RAZORPAY_KEY_ID}:{settings.RAZORPAY_KEY_SECRET}'.encode('utf-8')
    ).decode('ascii')
    body = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = Request(
        f'https://api.razorpay.com/v1/{endpoint.lstrip("/")}',
        data=body,
        headers={
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        method=method.upper(),
    )
    try:
        with urlopen(req, timeout=20) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        try:
            error_data = json.loads(exc.read().decode('utf-8'))
            description = error_data.get('error', {}).get('description') or 'Payment provider rejected the request.'
        except Exception:
            description = 'Payment provider rejected the request.'
        raise RuntimeError(description) from exc
    except URLError as exc:
        raise RuntimeError('Could not connect to the payment provider. Please try again.') from exc
    except (TimeoutError, ValueError) as exc:
        raise RuntimeError('The payment provider response could not be processed.') from exc


@login_required
@require_POST
def create_razorpay_order(request, invoice_id):
    if not hasattr(request.user, 'student'):
        return JsonResponse({'error': 'Only students can initiate fee payments.'}, status=403)

    try:
        with transaction.atomic():
            invoice = get_object_or_404(
                StudentFeeInvoice.objects.select_for_update().select_related('student', 'session'),
                pk=invoice_id,
                student=request.user.student,
            )
            invoice.recalculate_status()
            if invoice.status == StudentFeeInvoice.STATUS_CANCELLED:
                return JsonResponse({'error': 'This invoice has been cancelled.'}, status=400)

            amount_due = invoice.amount_due
            amount_paise = int((amount_due * 100).quantize(Decimal('1')))
            if amount_paise < 100:
                return JsonResponse({'error': 'There is no payable balance on this invoice.'}, status=400)

            existing_order = FeePayment.objects.filter(
                invoice=invoice,
                method=FeePayment.METHOD_RAZORPAY,
                status=FeePayment.STATUS_CREATED,
                gateway_order_id__isnull=False,
            ).order_by('-created_at').first()
            if existing_order:
                existing_paise = int((existing_order.amount * 100).quantize(Decimal('1')))
                if existing_paise != amount_paise:
                    return JsonResponse({'error': 'An online payment order is already pending for this invoice. Complete or resolve that payment before creating another.'}, status=409)
                response_data = {
                    'key': settings.RAZORPAY_KEY_ID,
                    'order_id': existing_order.gateway_order_id,
                    'amount': existing_paise,
                    'currency': settings.RAZORPAY_CURRENCY,
                    'name': 'College ERP',
                    'description': f'Fee payment - {invoice.invoice_number}',
                    'prefill': {
                        'name': invoice.student.name,
                        'email': invoice.student.email or request.user.email,
                        'contact': invoice.student.phone or '',
                    },
                    'payment_record_id': existing_order.pk,
                    'reused_order': True,
                }
                return JsonResponse(response_data, status=200)

            provider_order = _razorpay_request('POST', 'orders', {
                'amount': amount_paise,
                'currency': settings.RAZORPAY_CURRENCY,
                'receipt': invoice.invoice_number[:40],
                'notes': {'invoice_id': str(invoice.pk), 'student_id': str(invoice.student_id)},
            })
            payment = FeePayment.objects.create(
                invoice=invoice,
                amount=amount_due,
                method=FeePayment.METHOD_RAZORPAY,
                status=FeePayment.STATUS_CREATED,
                gateway_order_id=provider_order['id'],
                created_by=request.user,
            )
            response_data = {
                'key': settings.RAZORPAY_KEY_ID,
                'order_id': provider_order['id'],
                'amount': amount_paise,
                'currency': settings.RAZORPAY_CURRENCY,
                'name': 'College ERP',
                'description': f'Fee payment - {invoice.invoice_number}',
                'prefill': {
                    'name': invoice.student.name,
                    'email': invoice.student.email or request.user.email,
                    'contact': invoice.student.phone or '',
                },
                'payment_record_id': payment.pk,
            }
    except (RuntimeError, KeyError, IntegrityError) as exc:
        return JsonResponse({'error': str(exc) or 'Could not create a payment order.'}, status=502)

    return JsonResponse(response_data, status=201)


@login_required
@require_POST
def verify_razorpay_payment(request):
    if not hasattr(request.user, 'student'):
        return JsonResponse({'error': 'Only students can verify fee payments.'}, status=403)
    order_id = (request.POST.get('razorpay_order_id') or '').strip()
    payment_id = (request.POST.get('razorpay_payment_id') or '').strip()
    signature = (request.POST.get('razorpay_signature') or '').strip()
    if not all((order_id, payment_id, signature)):
        return JsonResponse({'error': 'Payment verification details are incomplete.'}, status=400)

    fee_payment = get_object_or_404(
        FeePayment.objects.select_related('invoice', 'invoice__student'),
        gateway_order_id=order_id,
        invoice__student=request.user.student,
        method=FeePayment.METHOD_RAZORPAY,
    )
    expected_signature = hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode('utf-8'),
        f'{order_id}|{payment_id}'.encode('utf-8'),
        hashlib.sha256,
    ).hexdigest() if settings.RAZORPAY_KEY_SECRET else ''
    if not expected_signature or not hmac.compare_digest(expected_signature, signature):
        return JsonResponse({'error': 'Payment signature verification failed.'}, status=400)

    if fee_payment.status == FeePayment.STATUS_SUCCESSFUL:
        if fee_payment.gateway_payment_id == payment_id:
            return JsonResponse({'message': 'Payment is already confirmed.', 'status': 'paid'}, status=200)
        return JsonResponse({'error': 'A different payment is already recorded for this order.'}, status=409)

    try:
        provider_payment = _razorpay_request('GET', f'payments/{quote(payment_id, safe="")}')
    except RuntimeError as exc:
        return JsonResponse({'error': str(exc)}, status=502)

    expected_amount = int((fee_payment.amount * 100).quantize(Decimal('1')))
    if provider_payment.get('order_id') != order_id:
        return JsonResponse({'error': 'The payment does not belong to this order.'}, status=400)
    if provider_payment.get('amount') != expected_amount or provider_payment.get('currency') != settings.RAZORPAY_CURRENCY:
        return JsonResponse({'error': 'The payment amount or currency does not match the invoice.'}, status=400)
    if provider_payment.get('status') != 'captured':
        return JsonResponse({
            'error': 'The provider has not captured this payment yet. Do not pay again; the payment will be confirmed by the provider webhook after capture.',
            'status': provider_payment.get('status', 'pending'),
        }, status=409)

    try:
        with transaction.atomic():
            # Always lock the invoice before its payment row. This serialises
            # simultaneous checkout callbacks/webhooks against one balance.
            invoice = StudentFeeInvoice.objects.select_for_update().get(pk=fee_payment.invoice_id)
            locked_payment = FeePayment.objects.select_for_update().select_related('invoice').get(pk=fee_payment.pk)
            if locked_payment.status == FeePayment.STATUS_SUCCESSFUL:
                if locked_payment.gateway_payment_id != payment_id:
                    return JsonResponse({'error': 'A different payment has already been applied to this invoice order.'}, status=409)
            else:
                locked_payment.gateway_payment_id = payment_id
                locked_payment.gateway_signature = signature
                locked_payment.gateway_payload = provider_payment
                locked_payment.status = FeePayment.STATUS_SUCCESSFUL
                locked_payment.verified_at = timezone.now()
                locked_payment.full_clean()
                locked_payment.save()
            invoice_status = invoice.recalculate_status()
    except (ValidationError, IntegrityError):
        return JsonResponse({'error': 'Payment was verified but could not be applied to the invoice. Contact the accounts office with the payment ID.'}, status=409)

    return JsonResponse({
        'message': 'Fee payment confirmed successfully.',
        'status': 'paid',
        'invoice_status': invoice_status,
    }, status=200)


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    if not settings.RAZORPAY_WEBHOOK_SECRET:
        return JsonResponse({'error': 'Webhook secret is not configured.'}, status=503)
    received_signature = request.headers.get('X-Razorpay-Signature', '')
    expected_signature = hmac.new(
        settings.RAZORPAY_WEBHOOK_SECRET.encode('utf-8'),
        request.body,
        hashlib.sha256,
    ).hexdigest()
    if not received_signature or not hmac.compare_digest(expected_signature, received_signature):
        return JsonResponse({'error': 'Invalid webhook signature.'}, status=400)

    try:
        event_data = json.loads(request.body.decode('utf-8'))
        event = event_data.get('event')
        entity = event_data.get('payload', {}).get('payment', {}).get('entity', {})
    except (ValueError, UnicodeDecodeError, AttributeError):
        return JsonResponse({'error': 'Invalid webhook payload.'}, status=400)

    if event not in {'payment.captured', 'payment.failed'}:
        return JsonResponse({'status': 'ignored'}, status=200)
    order_id = entity.get('order_id')
    payment_id = entity.get('id')
    if not order_id or not payment_id:
        return JsonResponse({'error': 'Webhook payment details are missing.'}, status=400)

    candidate = FeePayment.objects.filter(
        gateway_order_id=order_id,
        method=FeePayment.METHOD_RAZORPAY,
    ).only('id', 'invoice_id').first()
    if not candidate:
        return JsonResponse({'status': 'unrelated_order'}, status=200)

    try:
        with transaction.atomic():
            # Use the same invoice-then-payment lock order as checkout
            # verification to avoid races and deadlocks during reconciliation.
            invoice = StudentFeeInvoice.objects.select_for_update().get(pk=candidate.invoice_id)
            fee_payment = FeePayment.objects.select_for_update().select_related('invoice').get(pk=candidate.pk)
            expected_amount = int((fee_payment.amount * 100).quantize(Decimal('1')))
            if entity.get('amount') != expected_amount or entity.get('currency') != settings.RAZORPAY_CURRENCY:
                return JsonResponse({'error': 'Webhook amount/currency mismatch.'}, status=400)
            if event == 'payment.captured':
                if fee_payment.status == FeePayment.STATUS_SUCCESSFUL and fee_payment.gateway_payment_id not in {'', payment_id}:
                    return JsonResponse({'error': 'A different payment is already recorded for this order.'}, status=409)
                fee_payment.gateway_payment_id = payment_id
                fee_payment.status = FeePayment.STATUS_SUCCESSFUL
                fee_payment.verified_at = timezone.now()
            elif fee_payment.status != FeePayment.STATUS_SUCCESSFUL:
                fee_payment.status = FeePayment.STATUS_FAILED
            fee_payment.gateway_payload = event_data
            fee_payment.full_clean()
            fee_payment.save()
            invoice.recalculate_status()
    except FeePayment.DoesNotExist:
        return JsonResponse({'status': 'unrelated_order'}, status=200)
    except (ValidationError, IntegrityError):
        return JsonResponse({'error': 'Webhook could not be applied safely.'}, status=409)

    return JsonResponse({'status': 'processed'}, status=200)


@login_required
@never_cache
@user_passes_test(is_superuser)
def admin_fee_management(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Only the college administrator can manage fees.')

    structure_form = FeeStructureForm()
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'save_structure':
            structure_form = FeeStructureForm(request.POST)
            if structure_form.is_valid():
                try:
                    structure_form.save()
                    messages.success(request, 'Fee structure saved successfully.')
                    return redirect('admin_fee_management')
                except IntegrityError:
                    messages.error(request, 'A fee head with the same course, department, session and semester already exists.')
        elif action == 'generate_invoices':
            try:
                course = Course.objects.get(pk=request.POST.get('course_id'))
                department = Dept.objects.get(pk=request.POST.get('department_id'))
                session = Session.objects.get(pk=request.POST.get('session_id'))
                semester = int(request.POST.get('semester') or '')
            except (Course.DoesNotExist, Dept.DoesNotExist, Session.DoesNotExist, TypeError, ValueError):
                messages.error(request, 'Select a valid course, department, academic session and semester.')
                return redirect('admin_fee_management')

            if not course.duration_years or semester < 1 or semester > min(8, int(course.duration_years) * 2):
                messages.error(request, 'The selected semester is outside the course duration.')
                return redirect('admin_fee_management')
            canonical_session = normalize_session_name(session.name)
            session_parts = canonical_session.split('-')
            if len(session_parts) != 2 or not all(part.isdigit() for part in session_parts):
                messages.error(request, 'The selected session must use an academic-year format such as 2026-2027.')
                return redirect('admin_fee_management')
            session_start_year = int(session_parts[0])
            if int(session_parts[1]) not in {session_start_year + 1, (session_start_year + 1) % 100}:
                messages.error(request, 'The selected academic session has an invalid year range.')
                return redirect('admin_fee_management')
            expected_admission_year = session_start_year - ((semester - 1) // 2)
            structures = list(FeeStructure.objects.filter(
                course=course, department=department, session=session,
                semester=semester, is_active=True,
            ).order_by('fee_head'))
            if not structures:
                messages.error(request, 'No active fee heads exist for this course, department, session and semester. Create the fee structure first.')
                return redirect('admin_fee_management')

            students = Student.objects.filter(
                status__iexact='Approved',
                academic_status=Student.ACADEMIC_STATUS_ENROLLED,
                course=course,
                dept=department,
                admission_year=expected_admission_year,
                current_session=session,
                current_semester=semester,
            ).order_by('roll', 'id')
            created_count = 0
            existing_count = 0
            due_dates = [item.due_date for item in structures if item.due_date]
            due_date = min(due_dates) if due_dates else None
            total_amount = sum((item.amount for item in structures), Decimal('0.00'))
            with transaction.atomic():
                for student in students.iterator():
                    invoice, created = StudentFeeInvoice.objects.get_or_create(
                        student=student, session=session, semester=semester,
                        defaults={
                            'total_amount': total_amount,
                            'due_date': due_date,
                            'created_by': request.user,
                        },
                    )
                    if not created:
                        existing_count += 1
                        continue
                    StudentFeeInvoiceItem.objects.bulk_create([
                        StudentFeeInvoiceItem(
                            invoice=invoice,
                            fee_head=item.fee_head,
                            description=f'{course.name} / {department.name} / {session.name}',
                            amount=item.amount,
                        ) for item in structures
                    ])
                    created_count += 1
            messages.success(
                request,
                f'Invoice generation finished. Created: {created_count}; already existed: {existing_count}; matching students: {students.count()}.',
            )
            return redirect('admin_fee_management')
        elif action == 'record_offline_payment':
            method = request.POST.get('method')
            reference = (request.POST.get('reference') or '').strip()
            try:
                amount = Decimal(request.POST.get('amount') or '')
            except (InvalidOperation, TypeError, ValueError):
                messages.error(request, 'Enter a valid payment amount.')
                return redirect('admin_fee_management')
            allowed_methods = {FeePayment.METHOD_CASH, FeePayment.METHOD_UPI, FeePayment.METHOD_BANK, FeePayment.METHOD_CHEQUE}
            if method not in allowed_methods:
                messages.error(request, 'Select a valid offline payment method.')
                return redirect('admin_fee_management')
            if not reference:
                messages.error(request, 'Enter the cash receipt, UPI transaction or bank reference.')
                return redirect('admin_fee_management')
            try:
                with transaction.atomic():
                    invoice = get_object_or_404(
                        StudentFeeInvoice.objects.select_for_update(),
                        pk=request.POST.get('invoice_id'),
                    )
                    if FeePayment.objects.filter(
                        method=method, reference__iexact=reference,
                    ).exists():
                        raise ValidationError('This payment reference has already been recorded.')
                    if FeePayment.objects.filter(
                        invoice=invoice,
                        method=FeePayment.METHOD_RAZORPAY,
                        status=FeePayment.STATUS_CREATED,
                    ).exists():
                        raise ValidationError('This invoice has an unresolved online payment order. Verify or resolve it before recording an offline payment.')
                    if amount <= 0 or amount > invoice.amount_due:
                        raise ValidationError('Payment must be greater than zero and cannot exceed the current invoice balance.')
                    payment = FeePayment(
                        invoice=invoice, amount=amount, method=method,
                        status=FeePayment.STATUS_SUCCESSFUL, reference=reference,
                        created_by=request.user, verified_at=timezone.now(),
                    )
                    payment.full_clean()
                    payment.save()
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
                return redirect('admin_fee_management')
            messages.success(request, f'Offline payment recorded for {invoice.invoice_number}.')
            return redirect('admin_fee_management')
        else:
            messages.error(request, 'Invalid fee management action.')
            return redirect('admin_fee_management')

    return render(request, 'info/admin_fee_management.html', {
        'structure_form': structure_form,
        'fee_structures': FeeStructure.objects.select_related('course', 'department', 'session').order_by('-created_at')[:100],
        'invoices': StudentFeeInvoice.objects.select_related('student', 'session').order_by('-created_at')[:100],
        'recent_payments': FeePayment.objects.select_related('invoice', 'invoice__student').order_by('-created_at')[:100],
        'courses': Course.objects.order_by('name'),
        'departments': Dept.objects.order_by('name'),
        'sessions': Session.objects.order_by('-name'),
        'semester_choices': Subject.SEMESTER_CHOICES,
    })


@login_required
@require_POST
@never_cache
def manage_hod_assignments(request):
    if not request.user.is_superuser:
        return HttpResponseForbidden("You are not authorized to manage HOD assignments.")

    action = (request.POST.get('action') or '').strip()

    if action == 'save':
        teacher_id = request.POST.get('teacher_id')
        course_id = request.POST.get('course_id')
        department_id = request.POST.get('department_id')
        year_raw = request.POST.get('year')

        if not all([teacher_id, course_id, department_id, year_raw]):
            messages.error(request, 'Teacher, course, department and year are required.')
            return redirect('pending_requests_list')

        try:
            year = int(year_raw)
        except (TypeError, ValueError):
            messages.error(request, 'Invalid year selected.')
            return redirect('pending_requests_list')

        teacher = get_object_or_404(Teacher, id=teacher_id)
        course = get_object_or_404(Course, id=course_id)
        department = get_object_or_404(Dept, id=department_id)

        assignment = HODAssignment.objects.filter(
            course=course,
            department=department,
            year=year,
        ).first()

        if assignment:
            assignment.teacher = teacher
            assignment.is_active = True
            try:
                assignment.full_clean()
                assignment.save()
                messages.success(request, 'HOD assignment updated successfully.')
            except ValidationError as exc:
                messages.error(request, '; '.join(exc.messages))
            return redirect('pending_requests_list')

        try:
            new_assignment = HODAssignment(
                teacher=teacher,
                course=course,
                department=department,
                year=year,
                is_active=True,
            )
            new_assignment.full_clean()
            new_assignment.save()
            messages.success(request, 'HOD assigned successfully.')
        except ValidationError as exc:
            messages.error(request, '; '.join(exc.messages))
        return redirect('pending_requests_list')

    assignment_id = request.POST.get('assignment_id')
    if not assignment_id:
        messages.error(request, 'Assignment id is required.')
        return redirect('pending_requests_list')

    assignment = get_object_or_404(HODAssignment, id=assignment_id)

    if action == 'deactivate':
        assignment.is_active = False
        assignment.save(update_fields=['is_active'])
        messages.success(request, 'HOD assignment deactivated.')
        return redirect('pending_requests_list')

    if action == 'activate':
        assignment.is_active = True
        assignment.save(update_fields=['is_active'])
        messages.success(request, 'HOD assignment activated.')
        return redirect('pending_requests_list')

    if action == 'delete':
        assignment.delete()
        messages.success(request, 'HOD assignment deleted.')
        return redirect('pending_requests_list')

    messages.error(request, 'Invalid HOD assignment action.')
    return redirect('pending_requests_list')

def _course_allows_department(course, department):
    allowed_ids = set(course.departments.values_list('id', flat=True))
    if course.dept_id:
        allowed_ids.add(course.dept_id)
    return department.pk in allowed_ids


def register_student(request):
    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        email = (request.POST.get('email') or '').strip().lower()
        phone = (request.POST.get('phone') or '').strip()
        dob = request.POST.get('dob')
        roll_value = (request.POST.get('roll') or '').strip()
        sex = request.POST.get('sex')
        password = request.POST.get('password') or ''
        confirm = request.POST.get('confirm') or ''
        admission_year_value = request.POST.get('admission_year')
        photo = request.FILES.get('photo')

        try:
            course = Course.objects.get(pk=request.POST.get('course'))
            department = Dept.objects.get(pk=request.POST.get('department'))
            roll = int(roll_value)
            admission_year = int(admission_year_value)
            dob_value = date.fromisoformat(dob)
        except (Course.DoesNotExist, Dept.DoesNotExist, TypeError, ValueError):
            messages.error(request, 'Please provide a valid course, department, roll number, date of birth and admission year.')
            return redirect('register_student')

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, 'Enter a valid email address.')
            return redirect('register_student')
        if not name or len(phone) != 10 or not phone.isdigit():
            messages.error(request, 'Enter a name and valid 10-digit phone number.')
            return redirect('register_student')
        if not 2000 <= admission_year <= 2100:
            messages.error(request, 'Admission year must be between 2000 and 2100.')
            return redirect('register_student')
        if not 1 <= roll <= 9223372036854775807:
            messages.error(request, 'Roll number must be a positive 64-bit integer.')
            return redirect('register_student')
        if sex not in dict(sex_choice):
            messages.error(request, 'Select a valid gender option.')
            return redirect('register_student')
        if password != confirm:
            messages.error(request, 'Passwords do not match.')
            return redirect('register_student')
        if len(password) < 8:
            messages.error(request, 'Password must contain at least 8 characters.')
            return redirect('register_student')
        if (
            User.objects.filter(username=str(roll)).exists()
            or Student.objects.filter(roll=roll).exists()
            or PendingStudent.objects.filter(roll=roll).exists()
            or RejectedStudent.objects.filter(roll=roll).exists()
        ):
            messages.error(request, 'Roll number is already registered or awaiting an admin decision.')
            return redirect('register_student')
        if (
            User.objects.filter(email__iexact=email).exists()
            or Student.objects.filter(email__iexact=email).exists()
            or PendingStudent.objects.filter(email__iexact=email).exists()
            or RejectedStudent.objects.filter(email__iexact=email).exists()
        ):
            messages.error(request, 'Email address is already registered or awaiting an admin decision.')
            return redirect('register_student')
        if not _course_allows_department(course, department):
            messages.error(request, 'The selected department is not available for this course.')
            return redirect('register_student')
        if not course.duration_years or not 1 <= int(course.duration_years) <= 4:
            messages.error(request, 'The selected course must have a configured duration between one and four years. Contact the college office.')
            return redirect('register_student')

        duration = int(course.duration_years)
        sessions = generate_sessions_for_admission(admission_year, duration)
        current_session = cohort_session_for(admission_year, 1)
        if current_session is None:
            messages.error(request, 'Could not create the admission academic session.')
            return redirect('register_student')

        with transaction.atomic():
            pending = PendingStudent.objects.create(
                roll=roll,
                name=name,
                email=email,
                sex=sex,
                department=department,
                phone=phone,
                dob=dob_value,
                admission_year=admission_year,
                password=make_password(password),
                photo=photo,
                course=course,
                current_semester=1,
                current_session=current_session,
            )
            pending.sessions.set(sessions)

        messages.success(request, 'Registration request submitted. Your account will be activated after college approval.')
        return redirect('welcome')

    departments = Dept.objects.all().order_by('name')
    courses = Course.objects.all().order_by('name')
    sessions = Session.objects.all().order_by('-name')
    course_departments = {}
    for course in courses:
        depts_set = set()
        if course.dept:
            depts_set.add((course.dept.id, course.dept.name))
        for dept in course.departments.all():
            depts_set.add((dept.id, dept.name))
        course_departments[str(course.id)] = [
            {'id': id_, 'name': dept_name} for id_, dept_name in sorted(depts_set)
        ]
    return render(request, 'info/register_student.html', {
        'departments': departments,
        'sessions': sessions,
        'courses': courses,
        'current_year': date.today().year,
        'course_departments_json': json.dumps(course_departments),
    })


def register_teacher(request):
    if request.method == 'POST':
        name = (request.POST.get('name') or '').strip()
        email = (request.POST.get('email') or '').strip().lower()
        phone = (request.POST.get('phone') or '').strip()
        sex = request.POST.get('sex')
        photo = request.FILES.get('photo')
        password = request.POST.get('password') or ''
        confirm = request.POST.get('confirm') or ''
        try:
            course = Course.objects.get(pk=request.POST.get('course'))
            department = Dept.objects.get(pk=request.POST.get('department'))
            dob_value = date.fromisoformat(request.POST.get('dob') or '')
        except (Course.DoesNotExist, Dept.DoesNotExist, TypeError, ValueError):
            messages.error(request, 'Please provide a valid course, department and date of birth.')
            return redirect('register_teacher')

        try:
            validate_email(email)
        except ValidationError:
            messages.error(request, 'Enter a valid email address.')
            return redirect('register_teacher')
        if not name or len(phone) != 10 or not phone.isdigit():
            messages.error(request, 'Enter a name and valid 10-digit phone number.')
            return redirect('register_teacher')
        if sex not in dict(sex_choice):
            messages.error(request, 'Select a valid gender option.')
            return redirect('register_teacher')
        if password != confirm:
            messages.error(request, 'Passwords do not match.')
            return redirect('register_teacher')
        if len(password) < 8:
            messages.error(request, 'Password must contain at least 8 characters.')
            return redirect('register_teacher')
        if (
            User.objects.filter(email__iexact=email).exists()
            or Teacher.objects.filter(email__iexact=email).exists()
            or PendingTeacher.objects.filter(email__iexact=email).exists()
            or RejectedTeacher.objects.filter(email__iexact=email).exists()
        ):
            messages.error(request, 'Email address is already registered or awaiting an admin decision.')
            return redirect('register_teacher')
        if not _course_allows_department(course, department):
            messages.error(request, 'The selected department is not available for this course.')
            return redirect('register_teacher')

        username = teacher_username_for_email(email)
        if PendingTeacher.objects.filter(username__iexact=username).exists() or User.objects.filter(username__iexact=username).exists():
            messages.error(request, 'Unable to allocate a unique account username. Contact the college office.')
            return redirect('register_teacher')

        PendingTeacher.objects.create(
            username=username,
            dept=department,
            password=make_password(password),
            phone=phone,
            name=name,
            email=email,
            dob=dob_value,
            photo=photo,
            course=course,
            sex=sex,
        )
        messages.success(request, 'Teacher registration request submitted. Wait for college approval.')
        return redirect('welcome')

    departments = Dept.objects.all().order_by('name')
    courses = Course.objects.all().order_by('name')
    course_departments = {}
    for course in courses:
        depts_set = set()
        if course.dept:
            depts_set.add((course.dept.id, course.dept.name))
        for dept in course.departments.all():
            depts_set.add((dept.id, dept.name))
        course_departments[str(course.id)] = [
            {'id': id_, 'name': dept_name} for id_, dept_name in sorted(depts_set)
        ]
    return render(request, 'info/register_teacher.html', {
        'departments': departments,
        'courses': courses,
        'course_departments_json': json.dumps(course_departments),
    })

def welcome_landing(request):
    return render(request, 'info/welcome.html')

def change_pass_landing(request):
    form = PasswordChangeForm(request.user)
    admin_uploaded_notes = SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')
    pending_student = PendingStudent.objects.all()
    pending_teacher = PendingTeacher.objects.all()
    return render(request, 'info/change_password.html', {'form': form,        'admin_uploaded_notes': admin_uploaded_notes,
        'pending_student' : pending_student,
        'pending_teacher' : pending_teacher})
def edit_admin_profile_landing(request):
    form = AdminProfileForm(instance=request.user)
    admin_uploaded_notes = SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')
    pending_student = PendingStudent.objects.all()
    pending_teacher = PendingTeacher.objects.all()
    
    context = {
        'form': form,
        'modal_open': False,
        'form_errors': None,
        'admin_uploaded_notes': admin_uploaded_notes,
        'pending_student' : pending_student,
        'pending_teacher' : pending_teacher
    }
    return render(request, 'info/admin_profile.html', context)


@login_required
def edit_teacher_profile_landing(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can access this page.')

    form = TeacherProfileForm(instance=request.user.teacher)
    context = {
        'form': form,
    }
    return render(request, 'info/teacher_profile.html', context)


@login_required
def teacher_profile_update(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can update this profile.')

    teacher = request.user.teacher
    if request.method == 'POST':
        form = TeacherProfileForm(request.POST, instance=teacher)
        if form.is_valid():
            updated_teacher = form.save()
            if request.user.email != (updated_teacher.email or ''):
                request.user.email = updated_teacher.email or ''
                request.user.save(update_fields=['email'])
            messages.success(request, 'Profile updated successfully.')
            return redirect('teacher_profile')
        messages.error(request, 'Please fix the errors below.')
    else:
        form = TeacherProfileForm(instance=teacher)

    return render(request, 'info/teacher_profile.html', {'form': form})


@login_required
def edit_student_profile_landing(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can access this page.')

    form = StudentProfileForm(instance=request.user.student)
    context = {
        'form': form,
    }
    return render(request, 'info/student_profile.html', context)


@login_required
def student_profile_update(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can update this profile.')

    student = request.user.student
    if request.method == 'POST':
        form = StudentProfileForm(request.POST, instance=student)
        if form.is_valid():
            updated_student = form.save()
            if request.user.email != (updated_student.email or ''):
                request.user.email = updated_student.email or ''
                request.user.save(update_fields=['email'])
            messages.success(request, 'Profile updated successfully.')
            return redirect('student_profile')
        messages.error(request, 'Please fix the errors below.')
    else:
        form = StudentProfileForm(instance=student)

    return render(request, 'info/student_profile.html', {'form': form})
def course_add_landing(request):
    admin_uploaded_notes = SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')
    pending_student = PendingStudent.objects.all()
    pending_teacher = PendingTeacher.objects.all()
    context = {
        'admin_uploaded_notes': admin_uploaded_notes,
        'pending_student' : pending_student,
        'pending_teacher' : pending_teacher
    }
    return render(request, 'info/add_courses_admin.html', context)
def department_add_landing(request):
    courses = Course.objects.all()  # Always get courses
    admin_uploaded_notes = SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')
    pending_student = PendingStudent.objects.all()
    pending_teacher = PendingTeacher.objects.all()
    return render(request, 'info/department_adding.html', {'courses':courses,'admin_uploaded_notes': admin_uploaded_notes,
        'pending_student' : pending_student,
        'pending_teacher' : pending_teacher})
@login_required
@user_passes_test(is_superuser)
@never_cache
def rejected_request_landing(request):
    return redirect('rejected_requests_list')

@login_required
def homepage_teacher_landing(request):
    if not request.user.is_teacher:
        return redirect('index')

    teacher = request.user.teacher
    selected_session = request.POST.get('session') or request.GET.get('session') or ''
    selected_semester = request.POST.get('semester') or request.GET.get('semester') or ''
    selected_subject = request.POST.get('subject') or request.GET.get('subject') or ''

    if request.method == 'POST':
        if not (selected_session and selected_semester and selected_subject):
            messages.error(request, 'Please select session, semester and subject.')
            context = _build_attendance_modal_context(
                teacher,
                selected_session=selected_session,
                selected_semester=selected_semester,
                selected_subject=selected_subject,
                open_modal=True,
            )
            return render(request, 'info/t_homepage.html', context)

        try:
            semester_int = int(selected_semester)
        except (TypeError, ValueError):
            messages.error(request, 'Invalid semester selected.')
            context = _build_attendance_modal_context(
                teacher,
                selected_session=selected_session,
                selected_semester=selected_semester,
                selected_subject=selected_subject,
                open_modal=True,
            )
            return render(request, 'info/t_homepage.html', context)

        slot_qs = TimetableSlot.objects.filter(
            teacher=teacher,
            session_id=selected_session,
            semester=semester_int,
            subject_id=selected_subject,
        ).order_by('day', 'start_time')

        if not slot_qs.exists():
            messages.error(request, 'No timetable slot found for selected session, semester and subject.')
            context = _build_attendance_modal_context(
                teacher,
                selected_session=selected_session,
                selected_semester=selected_semester,
                selected_subject=selected_subject,
                open_modal=True,
            )
            return render(request, 'info/t_homepage.html', context)

        if slot_qs.count() > 1:
            messages.info(request, 'Multiple slots found. Opening attendance for the first slot.')

        slot = slot_qs.first()
        att_record, _ = AttendanceRecord.objects.get_or_create(
            timetable_slot=slot,
            date=timezone.now().date(),
            defaults={'session_held': True}
        )
        return redirect('t_attendance', att_record_id=att_record.id)

    open_modal = request.GET.get('open_attendance') == '1'
    context = _build_attendance_modal_context(
        teacher,
        selected_session=selected_session,
        selected_semester=selected_semester,
        selected_subject=selected_subject,
        open_modal=open_modal,
    )
    return render(request, 'info/t_homepage.html', context)

@never_cache
def approve_landing(request):
    # Canonical admin page is pending_requests_list; keep this as a compatibility entrypoint.
    return redirect('pending_requests_list')
def notice_landing(request):
    if not request.user.is_authenticated:
        return redirect('login')

    # Admin upload/management view
    if request.user.is_superuser:
        upload_form = SpecialUpdateNoteForm()
        admin_uploaded_notes = SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')
        return render(request, 'info/notices_upload.html', {
            'upload_form': upload_form,
            'admin_uploaded_notes': admin_uploaded_notes,
        })

    # Teacher/student read view
    latest_notice_id = SpecialUpdateNote.objects.order_by('-id').values_list('id', flat=True).first()
    state, _ = NoticeReadState.objects.get_or_create(user=request.user)
    state.last_seen_notice_id = int(latest_notice_id) if latest_notice_id else 0
    state.save(update_fields=['last_seen_notice_id', 'updated_at'])

    notes = SpecialUpdateNote.objects.all().order_by('-uploaded_at')
    return render(request, 'info/tshow_notice.html', {
        'special_update_notes': list(notes),
    })


@login_required
def chatroom(request):
    if request.method == 'POST':
        message_text = (request.POST.get('message') or '').strip()
        photo = request.FILES.get('photo')

        if photo and (not getattr(photo, 'content_type', '').startswith('image/')):
            messages.error(request, 'Only image files are allowed in chat.')
            return redirect('chatroom')

        if not message_text and not photo:
            messages.error(request, 'Send a message or upload a photo.')
            return redirect('chatroom')

        ChatMessage.objects.create(
            user=request.user,
            message=message_text,
            photo=photo,
        )

    # Mark chat as seen whenever user opens/posts in chatroom (persist across logins).
    chat_state, _ = ChatReadState.objects.get_or_create(user=request.user)
    chat_state.last_seen_message_at = timezone.now()
    chat_state.save(update_fields=['last_seen_message_at', 'updated_at'])

    chat_messages = ChatMessage.objects.select_related('user').order_by('created_at')
    if request.user.is_superuser:
        close_url = reverse('pending_requests_list')
    elif request.user.is_teacher:
        close_url = reverse('teacher_home')
    else:
        close_url = reverse('index')

    return render(request, 'info/chatroom.html', {
        'chat_messages': chat_messages,
        'close_url': close_url,
    })

@login_required
@require_POST
def delete_chat_message(request, message_id):
    msg = get_object_or_404(ChatMessage, id=message_id)

    if msg.user_id != request.user.id and not request.user.is_superuser:
        messages.error(request, 'You can only delete your own messages.')
        return redirect('chatroom')

    if msg.photo:
        msg.photo.delete(save=False)
    msg.photo = None
    msg.message = 'This message was deleted'
    msg.is_deleted = True
    msg.deleted_at = timezone.now()
    msg.save(update_fields=['photo', 'message', 'is_deleted', 'deleted_at'])
    messages.success(request, 'Message deleted.')
    return redirect('chatroom')


@login_required
@never_cache
def teacher_notes_landing(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can manage notes.')

    teacher = request.user.teacher
    teacher_subjects = Subject.objects.filter(teachers=teacher).select_related(
        'course', 'department', 'session'
    ).order_by('name')

    if request.method == 'POST':
        subject_id = request.POST.get('subject_id')
        title = (request.POST.get('title') or '').strip()
        description = (request.POST.get('description') or '').strip()
        note_file = request.FILES.get('file')

        if not subject_id or not title or not note_file:
            messages.error(request, 'Subject, title and file are required.')
            return redirect('teacher_notes')

        subject = teacher_subjects.filter(id=subject_id).first()
        if not subject:
            messages.error(request, 'Invalid subject selected.')
            return redirect('teacher_notes')

        TeacherNote.objects.create(
            teacher=teacher,
            subject=subject,
            title=title,
            description=description,
            file=note_file,
            course=subject.course,
            department=subject.department,
            session=subject.session,
            semester=subject.semester,
        )
        messages.success(request, 'Note uploaded successfully.')
        return redirect('teacher_notes')

    notes_qs = TeacherNote.objects.select_related(
        'teacher', 'subject', 'session', 'course', 'department'
    )

    selected_subject = (request.GET.get('subject') or '').strip()
    selected_session = (request.GET.get('session') or '').strip()
    selected_semester = (request.GET.get('semester') or '').strip()
    search_query = (request.GET.get('q') or '').strip()

    if selected_subject:
        notes_qs = notes_qs.filter(subject_id=selected_subject)
    if selected_session:
        notes_qs = notes_qs.filter(session_id=selected_session)
    if selected_semester:
        try:
            notes_qs = notes_qs.filter(semester=int(selected_semester))
        except ValueError:
            messages.warning(request, 'Invalid semester filter ignored.')
    if search_query:
        notes_qs = notes_qs.filter(Q(title__icontains=search_query) | Q(description__icontains=search_query))

    sessions = TeacherNote.objects.values('session__id', 'session__name').distinct().order_by('-session__name')
    semesters = TeacherNote.objects.values_list('semester', flat=True).distinct().order_by('semester')
    filter_subjects = Subject.objects.filter(teacher_notes__isnull=False).distinct().order_by('name')

    return render(request, 'info/teacher_notes.html', {
        'teacher_subjects': teacher_subjects,
        'filter_subjects': filter_subjects,
        'notes': notes_qs,
        'sessions': sessions,
        'semesters': semesters,
        'selected_subject': selected_subject,
        'selected_session': selected_session,
        'selected_semester': selected_semester,
        'search_query': search_query,
    })


@login_required
@never_cache
def student_notes_landing(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can view notes.')

    student = request.user.student
    student_subjects = student.subjects.all()

    base_notes_qs = TeacherNote.objects.select_related(
        'teacher', 'subject', 'session', 'course', 'department'
    ).filter(subject__in=student_subjects).distinct()
    notes_qs = base_notes_qs

    selected_subject = (request.GET.get('subject') or '').strip()
    selected_session = (request.GET.get('session') or '').strip()
    selected_semester = (request.GET.get('semester') or '').strip()
    search_query = (request.GET.get('q') or '').strip()

    if selected_subject:
        notes_qs = notes_qs.filter(subject_id=selected_subject)
    if selected_session:
        notes_qs = notes_qs.filter(session_id=selected_session)
    if selected_semester:
        try:
            notes_qs = notes_qs.filter(semester=int(selected_semester))
        except ValueError:
            messages.warning(request, 'Invalid semester filter ignored.')
    if search_query:
        notes_qs = notes_qs.filter(
            Q(title__icontains=search_query) | Q(description__icontains=search_query)
        )

    filter_subjects = Subject.objects.filter(
        id__in=student_subjects.values_list('id', flat=True),
        teacher_notes__isnull=False,
    ).distinct().order_by('name')
    sessions = base_notes_qs.values('session__id', 'session__name').distinct().order_by('-session__name')
    semesters = base_notes_qs.values_list('semester', flat=True).distinct().order_by('semester')

    return render(request, 'info/student_notes.html', {
        'student': student,
        'filter_subjects': filter_subjects,
        'notes': notes_qs,
        'sessions': sessions,
        'semesters': semesters,
        'selected_subject': selected_subject,
        'selected_session': selected_session,
        'selected_semester': selected_semester,
        'search_query': search_query,
    })


@login_required
def student_course_teachers(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can view this page.')

    student = request.user.student
    teachers = Teacher.objects.filter(
        course=student.course, status__iexact='approved'
    ).select_related('dept', 'course').order_by('name')

    return render(request, 'info/student_teachers.html', {
        'student': student,
        'teachers': teachers,
    })


@login_required
@require_POST
def delete_teacher_note(request, note_id):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can delete notes.')

    note = get_object_or_404(TeacherNote, id=note_id, teacher=request.user.teacher)
    if note.file:
        note.file.delete(save=False)
    note.delete()
    messages.success(request, 'Note deleted successfully.')
    return redirect('teacher_notes')


@login_required
@never_cache
def teacher_assignments_landing(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can manage assignments.')

    teacher = request.user.teacher
    teacher_subjects = Subject.objects.filter(teachers=teacher).select_related(
        'course', 'department', 'session'
    ).order_by('name')

    if request.method == 'POST':
        subject_id = request.POST.get('subject_id')
        title = (request.POST.get('title') or '').strip()
        description = (request.POST.get('description') or '').strip()
        due_date_raw = (request.POST.get('due_date') or '').strip()
        assignment_file = request.FILES.get('assignment_file')

        if not subject_id or not title or not assignment_file:
            messages.error(request, 'Subject, title and assignment file are required.')
            return redirect('teacher_assignments')

        subject = teacher_subjects.filter(id=subject_id).first()
        if not subject:
            messages.error(request, 'Invalid subject selected.')
            return redirect('teacher_assignments')

        due_date_value = None
        if due_date_raw:
            try:
                due_date_value = date.fromisoformat(due_date_raw)
            except ValueError:
                messages.error(request, 'Invalid due date format.')
                return redirect('teacher_assignments')

        SubjectAssignment.objects.create(
            teacher=teacher,
            subject=subject,
            title=title,
            description=description,
            assignment_file=assignment_file,
            due_date=due_date_value,
            course=subject.course,
            department=subject.department,
            session=subject.session,
            semester=subject.semester,
        )
        messages.success(request, 'Assignment uploaded successfully.')
        return redirect('teacher_assignments')

    assignments = SubjectAssignment.objects.filter(teacher=teacher).select_related(
        'subject', 'session', 'course', 'department'
    ).prefetch_related('submissions__student').order_by('-created_at')

    return render(request, 'info/teacher_assignments.html', {
        'teacher_subjects': teacher_subjects,
        'assignments': assignments,
    })


@login_required
@never_cache
def student_assignments_landing(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can view assignments.')

    student = request.user.student
    student_subjects = student.subjects.all()

    assignments = SubjectAssignment.objects.filter(
        subject__in=student_subjects
    ).select_related('teacher', 'subject', 'session').order_by('-created_at').distinct()

    submissions = AssignmentSubmission.objects.filter(
        student=student,
        assignment__in=assignments,
    ).select_related('assignment')
    submissions_by_assignment = {sub.assignment_id: sub for sub in submissions}
    assignment_rows = [
        {'assignment': assignment, 'submission': submissions_by_assignment.get(assignment.id)}
        for assignment in assignments
    ]

    return render(request, 'info/student_assignments.html', {
        'assignment_rows': assignment_rows,
    })


@login_required
@require_POST
def submit_assignment(request, assignment_id):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can submit assignments.')

    student = request.user.student
    assignment = get_object_or_404(SubjectAssignment, id=assignment_id)

    if not student.subjects.filter(id=assignment.subject_id).exists():
        return HttpResponseForbidden('You are not allowed to submit this assignment.')

    submission_file = request.FILES.get('submission_file')
    if not submission_file:
        messages.error(request, 'Please upload a PDF file.')
        return redirect('student_assignments')

    if not submission_file.name.lower().endswith('.pdf'):
        messages.error(request, 'Only PDF files are allowed.')
        return redirect('student_assignments')

    AssignmentSubmission.objects.update_or_create(
        assignment=assignment,
        student=student,
        defaults={'submission_file': submission_file},
    )
    messages.success(request, f'Assignment submitted to {assignment.teacher.name}.')
    return redirect('student_assignments')


def add_subject_view(request, teacher_id):
    if not request.user.is_teacher or request.user.teacher.id != int(teacher_id):
        return HttpResponseForbidden()

    session_filter = request.GET.get('session', None)
    semester_filter = request.GET.get('semester', None)
    name = request.GET.get('name')
    teacher = request.user.teacher
    teacher_assignments = get_active_hod_assignments_for_teacher(teacher)

    if not teacher_assignments.exists():
        messages.error(request, 'You are not assigned as HOD for any course/department/year yet.')
        return redirect('teacher_home')

    if request.method == "POST":
        name = request.POST.get("subject_name")
        code = request.POST.get("subject_code")
        typ = request.POST.get("subject_type")
        sess_id = request.POST.get("session")
        semester = request.POST.get("semester")
        selected_teacher_ids = request.POST.getlist('teachers')

        if not all([name, code, typ, selected_teacher_ids, sess_id, semester]):
            messages.error(request, "All fields are required.")
            return redirect("add_subject", teacher_id=request.user.teacher.id)

        year = semester_to_year(semester)
        if not year:
            messages.error(request, 'Invalid semester selected.')
            return redirect("add_subject", teacher_id=request.user.teacher.id)

        hod_assignment = teacher_assignments.filter(
            course=teacher.course,
            department=teacher.dept,
            year=year,
        ).first()

        if not hod_assignment:
            messages.error(request, 'You are not assigned as HOD for this year, so subject creation is not allowed.')
            return redirect("add_subject", teacher_id=request.user.teacher.id)

        try:
            subject = Subject.objects.create(
                name=name,
                code=code,
                type=typ,
                session_id=sess_id,
                semester=semester,
                department=hod_assignment.department,
                course=hod_assignment.course,
            
            )
            if selected_teacher_ids:
                subject.teachers.set(selected_teacher_ids)
            students = Student.objects.filter(
            current_semester=semester,
            dept=hod_assignment.department,
            course=hod_assignment.course,
            current_session_id=sess_id,
            )
            for student in students:
                student.subjects.add(subject)
        
            messages.success(request, f"Subject '{name}' added successfully.")
        except Exception as e:
            messages.error(request, f"Error adding subject: {e}")

        return redirect("add_subject", teacher_id=request.user.teacher.id)

    # GET request: fetch and display subjects grouped by department and session
    scope_query = Q()
    for assignment in teacher_assignments:
        sem_start = (assignment.year * 2) - 1
        sem_end = assignment.year * 2
        scope_query |= Q(
            course=assignment.course,
            department=assignment.department,
            semester__gte=sem_start,
            semester__lte=sem_end,
        )

    subjects = Subject.objects.select_related('department', 'session').filter(scope_query)
    semester_choices = Subject.SEMESTER_CHOICES
    if session_filter:
        subjects = subjects.filter(session=session_filter)
    if semester_filter:
        subjects = subjects.filter(semester=semester_filter)
    if name:
        subjects = subjects.filter(name__icontains=name)

    return render(request, "info/add_subjects.html", {
        "departments": Dept.objects.all(),
        "sessions": Session.objects.all().order_by("-name"),
        # "subjects_by_dept_sess": subjects_by_dept_sess,
        "subjects": subjects,
        'semester': semester_choices,  
        'selected_session': session_filter,
        "selected_semester": semester_filter,
    })

def delete_subject(request, subject_id):
    if not request.user.is_teacher:
        return HttpResponseForbidden()

    subject = get_object_or_404(Subject, id=subject_id)
    teacher = request.user.teacher
    year = semester_to_year(subject.semester)
    is_allowed = HODAssignment.objects.filter(
        teacher=teacher,
        course=subject.course,
        department=subject.department,
        year=year,
        is_active=True,
    ).exists()

    if not is_allowed:
        return HttpResponseForbidden('You are not allowed to delete this subject.')

    subject.teachers.clear()
    subject.delete()
    messages.success(request, f"Subject '{subject.name}' deleted successfully.")
    return redirect("add_subject", teacher_id=request.user.teacher.id)

def subject_teachers_view(request, subject_id):
    subject = get_object_or_404(Subject, id=subject_id)
    assigned_teachers = subject.teachers.all()
    active_hod_rows = HODAssignment.objects.filter(
        teacher__in=assigned_teachers,
        is_active=True,
    ).select_related('course', 'department', 'teacher')

    hod_tags_by_teacher = {}
    for assignment in active_hod_rows:
        label = f"{assignment.department.name} - Year {assignment.year} ({assignment.course.name})"
        hod_tags_by_teacher.setdefault(assignment.teacher_id, []).append(label)

    return render(request, 'info/assigned_teachers_partial.html', {
        'assigned_teachers': assigned_teachers,
        'hod_tags_by_teacher': hod_tags_by_teacher,
    })


def get_subjects_for_student(dept_id, sess_id):
    return Subject.objects.filter(department_id=dept_id, session_id=sess_id)


def teachers_by_department(request):
    if not request.user.is_authenticated or not hasattr(request.user, 'teacher'):
        return JsonResponse({'teachers': []})

    teacher = request.user.teacher
    teacher_assignments = get_active_hod_assignments_for_teacher(teacher)
    if not teacher_assignments.exists():
        return JsonResponse({'teachers': []})

    pair_query = Q()
    for assignment in teacher_assignments:
        pair_query |= Q(dept=assignment.department, course=assignment.course)

    teachers = Teacher.objects.filter(pair_query, status__iexact='Approved').values('id', 'name')
    teacher_list = list(teachers)
    return JsonResponse({'teachers': teacher_list})


@login_required
@user_passes_test(is_superuser)
@require_POST
def delete_rejected_teacher(request, teacher_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden()

    teacher = get_object_or_404(RejectedTeacher, id=teacher_id)
    teacher.delete()
    messages.success(request, "Rejected teacher record deleted successfully.")
    return redirect('rejected_request_landing') 

@login_required
@user_passes_test(is_superuser)
@require_POST
def delete_rejected_student(request, student_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden()

    student = get_object_or_404(RejectedStudent, id=student_id)
    student.delete()
    messages.success(request, "Rejected Student record deleted successfully.")
    return redirect('rejected_request_landing') 

def add_course(request):
    if request.method == 'POST':
        name = request.POST.get('name', '').strip()
        shortname = request.POST.get('shortname', 'X').strip() or 'X'
        dept_id = request.POST.get('department_short_name', '').strip()
        department_name = request.POST.get('department_name', '').strip()
        duration_years = request.POST.get('duration_years', None)

        # Basic validation
        if not name:
            messages.error(request, 'Course name is required.')
        else:
            if department_name and dept_id:
                department, created = Dept.objects.get_or_create(
                id=dept_id,
                defaults={'name': department_name}
            )
            elif department_name:
                generated_id = slugify(department_name)
                department, created = Dept.objects.get_or_create(
                id=generated_id,
                defaults={'name': department_name}
            )
            else:
                department = None
            try:
                duration_years_int = int(duration_years) if duration_years else None
            except ValueError:
                duration_years_int = None
            
            exists = Course.objects.filter(
                Q(name__iexact=name)
            ).exists()

            if exists:
                messages.warning(request, f'Course "{name}" already exists in this department.')
                return redirect('course_add')

            
            # Create course
            Course.objects.create(
                dept=department,
                name=name,
                shortname=shortname,
                duration_years=duration_years_int,
            )
            messages.success(request, f'Course "{name}" added successfully.')
            return redirect('course_add')  # Change redirect target as needed

    return render(request, 'info/add_courses_admin.html')

def add_department_to_course(request):
    courses = Course.objects.all()  # Always get courses
    if request.method == 'POST':
        course_id = request.POST.get('course')
        department_name = request.POST.get('department_name', '').strip()
        department_short_name = request.POST.get('department_short_name', '').strip()

        if not course_id:
            messages.error(request, "Please select a course.")
            return render(request, 'info/department_adding.html', {'courses': courses})

        if not department_name or not department_short_name:
            messages.error(request, "Both department name and short name are required.")
            return render(request, 'info/department_adding.html', {'courses': courses})

        course = get_object_or_404(Course, id=course_id)

        dept_id = (department_short_name)
        if not dept_id:
            dept_id = department_short_name.replace(' ', '_').upper()

        department, created = Dept.objects.get_or_create(
            id=dept_id,
            defaults={'name': department_name}
        )

        if not created and department.name != department_name:
            department.name = department_name
            department.save()

        if course.departments.filter(id=department.id).exists():
            messages.warning(
        request,
        f"Department '{department_name}' is already assigned to course '{course.name}'."
    )
        else:
            course.departments.add(department)
            messages.success(
        request,
        f"Department '{department_name}' added to course '{course.name}' successfully."
    )

        return redirect('add_department_to_course')  # ✅ keep redirect only after success

    # For GET request
    return render(request, 'info/department_adding.html', {'courses': courses})

@login_required
def upload_special_update(request):
    if request.method == 'POST':
        form = SpecialUpdateNoteForm(request.POST, request.FILES)
        if form.is_valid():
            note = form.save(commit=False)
            note.uploader = request.user
            note.save()
            messages.success(request, "Special updates/notice uploaded successfully.")
            return redirect('special_updates_list')  # Prevent resubmission on page refresh
        else:
            messages.error(request, "There was an error uploading the file. Please check the form.")
            upload_form = form
    else:
        upload_form = SpecialUpdateNoteForm()

    admin_uploaded_notes = SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')

    context = {
        'upload_form': upload_form,
        'admin_uploaded_notes': SpecialUpdateNote.objects.filter(uploader=request.user).order_by('-uploaded_at')
    }
    return render(request, 'info/notices_upload.html', context)

@login_required
def delete_special_update(request, pk):
    if request.method == 'POST':
        note = get_object_or_404(SpecialUpdateNote, pk=pk, uploader=request.user)
        note.pdf_file.delete(save=False)  # delete the file
        note.delete()  # delete DB record
        messages.success(request, "File deleted successfully.")
    return redirect('special_updates_list')

@login_required
def admin_profile(request):
    if request.method == 'POST':
        form = AdminProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Profile updated successfully.")
            return redirect('admin_profile')
    else:
        form = AdminProfileForm(instance=request.user)

    return render(request, 'info/admin_profile.html', {'form': form})


@login_required
def admin_change_password(request):
    if request.method == 'POST':
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            update_session_auth_hash(request, user)
            logout(request)  # Log out the user immediately
            messages.success(request, 'Password changed successfully. Please log in again.')
            return redirect('login')  # Redirect to login page after logout
    else:
        form = PasswordChangeForm(request.user)
    cancel_url_name = 'admin_profile'
    if getattr(request.user, 'is_teacher', False):
        cancel_url_name = 'teacher_profile'
    elif getattr(request.user, 'is_student', False):
        cancel_url_name = 'student_profile'

    return render(request, 'info/change_password.html', {
        'form': form,
        'cancel_url_name': cancel_url_name,
    })

def get_new_notice_flag(user):
    last_login = user.last_login or timezone.now()
    return SpecialUpdateNote.objects.filter(uploaded_at__gt=last_login).exists()

from django.db.models import Q


@login_required
@require_GET
def admin_student_report_pdf(request, student_id):
    """Admin-only comprehensive student report across subjects, attendance, and exam marks."""
    is_student_self = hasattr(request.user, 'student') and request.user.student.id == student_id
    if not (request.user.is_superuser or request.user.is_staff or is_student_self):
        return HttpResponseForbidden('Only admins can download this report.')

    student = get_object_or_404(Student.objects.select_related('dept', 'course', 'current_session'), id=student_id)

    subjects = student.subjects.select_related('session', 'course', 'department').order_by('session__name', 'semester', 'name')

    assignment_totals_raw = SubjectAssignment.objects.filter(subject__in=subjects).values('subject_id').annotate(total=Count('id'))
    assignment_submitted_raw = AssignmentSubmission.objects.filter(
        student=student,
        assignment__subject__in=subjects,
    ).values('assignment__subject_id').annotate(total=Count('id'))
    assignment_total_by_subject = {row['subject_id']: row['total'] for row in assignment_totals_raw}
    assignment_submitted_by_subject = {row['assignment__subject_id']: row['total'] for row in assignment_submitted_raw}

    # Attendance aggregation per subject
    attendance_qs = StudentAttendance.objects.filter(
        student=student,
        attendance_record__session_held=True,
    ).values(
        'attendance_record__timetable_slot__subject_id',
        'status',
    )
    attendance_map = defaultdict(lambda: {'total': 0, 'attended': 0})
    for row in attendance_qs:
        sid = row['attendance_record__timetable_slot__subject_id']
        attendance_map[sid]['total'] += 1
        if row['status']:
            attendance_map[sid]['attended'] += 1

    conducted_exams = list(
        Exam.objects.filter(
            subject__in=subjects,
            status='Conducted',
        ).select_related('subject', 'session').order_by('subject__name', 'exam_type', 'date_conducted')
    )
    marks_lookup = {
        em.exam_id: em
        for em in ExamMark.objects.filter(student=student, exam__in=conducted_exams)
    }

    subject_summary_rows = []
    exam_detail_rows = []

    missing_attendance_count = 0
    missing_marks_count = 0
    absent_exam_count = 0
    low_attendance_count = 0
    eligible_count = 0
    pass_count = 0
    fail_count = 0

    for subject in subjects:
        attendance_total = attendance_map[subject.id]['total']
        attendance_attended = attendance_map[subject.id]['attended']
        attendance_pct = round((attendance_attended / attendance_total) * 100, 2) if attendance_total else None
        assignment_total = assignment_total_by_subject.get(subject.id, 0)
        assignment_submitted = assignment_submitted_by_subject.get(subject.id, 0)
        assignment_pending = max(assignment_total - assignment_submitted, 0)
        assignment_submission_pct = round((assignment_submitted / assignment_total) * 100, 2) if assignment_total else None

        subject_exams = [e for e in conducted_exams if e.subject_id == subject.id]
        marks_entered = 0
        marks_sum = 0
        marks_max = 0
        missing_marks = False
        has_absent_exam = False

        for exam in subject_exams:
            mark_entry = marks_lookup.get(exam.id)
            if not mark_entry:
                missing_marks = True
                exam_mark_text = 'N/A'
            elif mark_entry.is_absent:
                marks_entered += 1
                marks_max += exam.total_marks
                exam_mark_text = 'AB'
                has_absent_exam = True
            else:
                mark_value = mark_entry.marks_obtained if mark_entry.marks_obtained is not None else 0
                marks_entered += 1
                marks_sum += mark_value
                marks_max += exam.total_marks
                exam_mark_text = str(mark_value)

            exam_detail_rows.append({
                'subject': subject.name,
                'session': exam.session.name,
                'semester': subject.semester,
                'exam_type': exam.exam_type,
                'exam_date': exam.date_conducted.strftime('%d-%b-%Y') if exam.date_conducted else 'Not set',
                'exam_time': exam.exam_time.strftime('%I:%M %p') if exam.exam_time else 'Not set',
                'total_marks': exam.total_marks,
                'marks_text': exam_mark_text,
            })

        average_pct = round((marks_sum / marks_max) * 100, 2) if marks_max else None
        performance_band = _performance_band(average_pct)
        low_attendance = attendance_pct is not None and attendance_pct < 75
        eligibility = attendance_pct is not None and attendance_pct >= 75 and (not missing_marks)

        flags = []
        if attendance_total == 0:
            flags.append('Missing Attendance')
        if missing_marks and subject_exams:
            flags.append('Missing Marks')
        if has_absent_exam:
            flags.append('Absent In Exam')
        if low_attendance and average_pct is not None and average_pct < 40:
            flags.append('Low Attendance + Low Marks')

        if 'Missing Attendance' in flags:
            missing_attendance_count += 1
        if 'Missing Marks' in flags:
            missing_marks_count += 1
        if has_absent_exam:
            absent_exam_count += 1
        if low_attendance:
            low_attendance_count += 1
        if eligibility:
            eligible_count += 1
        if average_pct is not None:
            if average_pct >= 40:
                pass_count += 1
            else:
                fail_count += 1

        subject_summary_rows.append({
            'subject': subject.name,
            'session': subject.session.name,
            'semester': subject.semester,
            'attendance_text': f"{attendance_attended}/{attendance_total}" if attendance_total else 'N/A',
            'attendance_pct': attendance_pct,
            'exam_count': len(subject_exams),
            'marks_entered': marks_entered,
            'total_text': f"{marks_sum}/{marks_max}" if marks_max else 'N/A',
            'average_pct': average_pct,
            'performance_band': performance_band,
            'eligibility': 'Eligible' if eligibility else 'Not Eligible',
            'flags_text': ', '.join(flags) if flags else '-',
            'assignment_total': assignment_total,
            'assignment_submitted': assignment_submitted,
            'assignment_pending': assignment_pending,
            'assignment_submission_pct': assignment_submission_pct,
        })

    overall_exam_count = len(conducted_exams)
    overall_entered = sum(1 for e in conducted_exams if e.id in marks_lookup)
    overall_subject_avg_values = [r['average_pct'] for r in subject_summary_rows if r['average_pct'] is not None]
    overall_average_pct = round(sum(overall_subject_avg_values) / len(overall_subject_avg_values), 2) if overall_subject_avg_values else None

    overall_classes = sum(attendance_map[s.id]['total'] for s in subjects)
    overall_attended = sum(attendance_map[s.id]['attended'] for s in subjects)
    overall_attendance_pct = round((overall_attended / overall_classes) * 100, 2) if overall_classes else None
    overall_assignments_total = sum(r['assignment_total'] for r in subject_summary_rows)
    overall_assignments_submitted = sum(r['assignment_submitted'] for r in subject_summary_rows)
    overall_assignments_pending = max(overall_assignments_total - overall_assignments_submitted, 0)
    overall_assignment_submission_pct = round((overall_assignments_submitted / overall_assignments_total) * 100, 2) if overall_assignments_total else None

    generated_at = timezone.localtime().strftime('%Y-%m-%d %I:%M %p')

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'AdminStudentTitle',
        parent=styles['Title'],
        fontName='Helvetica-Bold',
        fontSize=20,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#0f172a'),
        spaceAfter=8,
    )
    section_style = ParagraphStyle(
        'AdminStudentSection',
        parent=styles['Heading4'],
        fontName='Helvetica-Bold',
        fontSize=11,
        textColor=colors.HexColor('#1d4ed8'),
        spaceBefore=5,
        spaceAfter=4,
    )
    normal_small = ParagraphStyle(
        'AdminStudentNormalSmall',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#111827'),
    )

    elements = []
    elements.append(Paragraph('Comprehensive Student Report', title_style))
    subtitle = (
        f"{student.name} | "
        f"{student.dept.name if student.dept else '-'} | "
        f"{student.course.name if student.course else '-'}"
    )
    elements.append(Paragraph(subtitle, ParagraphStyle(
        'AdminStudentSubTitle',
        parent=normal_small,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#4b5563'),
        spaceAfter=2,
    )))
    elements.append(Paragraph(f"Generated At: {generated_at}", ParagraphStyle(
        'AdminStudentGeneratedAt',
        parent=normal_small,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#4b5563'),
        spaceAfter=6,
    )))

    photo_cell = Paragraph('No Photo', normal_small)
    if student.photo and hasattr(student.photo, 'path'):
        try:
            photo_cell = Image(student.photo.path, width=30 * mm, height=34 * mm)
        except Exception:
            pass

    profile_table = Table([
        [Paragraph('<b>Student Name</b>', normal_small), Paragraph(student.name, normal_small)],
        [Paragraph('<b>Roll</b>', normal_small), Paragraph(str(student.roll or '-'), normal_small)],
        [Paragraph('<b>Department</b>', normal_small), Paragraph(str(student.dept.name if student.dept else '-'), normal_small)],
        [Paragraph('<b>Course</b>', normal_small), Paragraph(str(student.course.name if student.course else '-'), normal_small)],
        [Paragraph('<b>Current Session</b>', normal_small), Paragraph(str(student.current_session.name if student.current_session else '-'), normal_small)],
        [Paragraph('<b>Current Semester</b>', normal_small), Paragraph(str(student.current_semester or '-'), normal_small)],
        [Paragraph('<b>Email</b>', normal_small), Paragraph(student.email or '-', normal_small)],
    ], colWidths=[42 * mm, 106 * mm])
    profile_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#eff6ff')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#1e3a8a')),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#cbd5e1')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))

    header_block = Table([[photo_cell, profile_table]], colWidths=[33 * mm, 153 * mm])
    header_block.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, 0), 'CENTER'),
        ('BOX', (0, 0), (-1, -1), 0.6, colors.HexColor('#cbd5e1')),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(header_block)
    elements.append(Spacer(1, 4 * mm))

    elements.append(Paragraph('Family Details', section_style))
    family_data = [
        [Paragraph('<b>Father Name</b>', normal_small), Paragraph(student.father_name or '-', normal_small)],
        [Paragraph('<b>Mother Name</b>', normal_small), Paragraph(student.mother_name or '-', normal_small)],
        [Paragraph('<b>Guardian Name</b>', normal_small), Paragraph(student.guardian_name or '-', normal_small)],
        [Paragraph('<b>Guardian Relation</b>', normal_small), Paragraph(student.guardian_relation or '-', normal_small)],
        [Paragraph('<b>Guardian Phone</b>', normal_small), Paragraph(student.guardian_phone or '-', normal_small)],
        [Paragraph('<b>Family Address</b>', normal_small), Paragraph(student.family_address or '-', normal_small)],
        [Paragraph('<b>City</b>', normal_small), Paragraph(student.family_city or '-', normal_small)],
        [Paragraph('<b>State</b>', normal_small), Paragraph(student.family_state or '-', normal_small)],
        [Paragraph('<b>Pincode</b>', normal_small), Paragraph(student.family_pincode or '-', normal_small)],
    ]
    family_table = Table(family_data, colWidths=[42 * mm, 144 * mm])
    family_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#eff6ff')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#1e3a8a')),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#cbd5e1')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))
    elements.append(family_table)
    elements.append(Spacer(1, 4 * mm))

    elements.append(Paragraph('Performance Snapshot', section_style))
    metrics_data = [
        ['Overall Attendance', f"{overall_attendance_pct if overall_attendance_pct is not None else 'N/A'}% ({overall_attended}/{overall_classes})"],
        ['Overall Average %', f"{overall_average_pct if overall_average_pct is not None else 'N/A'}"],
        ['Subjects / Exams / Marks', f"{len(subject_summary_rows)} / {overall_exam_count} / {overall_entered}"],
        ['Assignments', f"Total: {overall_assignments_total} | Submitted: {overall_assignments_submitted} | Pending: {overall_assignments_pending} | Submission %: {overall_assignment_submission_pct if overall_assignment_submission_pct is not None else 'N/A'}"],
        ['Eligibility', f"Eligible: {eligible_count} | Not Eligible: {max(len(subject_summary_rows) - eligible_count, 0)}"],
        ['Pass / Fail', f"Pass: {pass_count} | Fail: {fail_count}"],
        ['Risk Flags', f"Low Attendance: {low_attendance_count}, Missing Attendance: {missing_attendance_count}, Missing Marks: {missing_marks_count}, Absent In Exam: {absent_exam_count}"],
    ]
    metrics_table = Table(metrics_data, colWidths=[48 * mm, 138 * mm])
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#fef3c7')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#92400e')),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#d1d5db')),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    elements.append(metrics_table)
    elements.append(Spacer(1, 4 * mm))

    elements.append(Paragraph('Subject-wise Performance', section_style))

    subject_header = [
        'Subject', 'Session', 'Sem', 'Attendance', 'Att %',
        'Exams', 'Marked', 'Total', 'Avg %', 'Band', 'Eligibility', 'Flags'
    ]
    subject_data = [subject_header]
    for row in subject_summary_rows:
        subject_data.append([
            row['subject'],
            row['session'],
            str(row['semester']),
            row['attendance_text'],
            str(row['attendance_pct']) if row['attendance_pct'] is not None else 'N/A',
            str(row['exam_count']),
            str(row['marks_entered']),
            row['total_text'],
            str(row['average_pct']) if row['average_pct'] is not None else 'N/A',
            row['performance_band'],
            row['eligibility'],
            row['flags_text'],
        ])

    subj_col_widths = [24 * mm, 18 * mm, 8 * mm, 16 * mm, 11 * mm, 10 * mm, 10 * mm, 14 * mm, 11 * mm, 12 * mm, 18 * mm, 34 * mm]
    subject_table = Table(subject_data, repeatRows=1, colWidths=subj_col_widths)
    subject_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0f766e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#9ca3af')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f9fafb'), colors.HexColor('#f3f4f6')]),
        ('ALIGN', (2, 1), (9, -1), 'CENTER'),
    ]))
    elements.append(subject_table)

    elements.append(Spacer(1, 4 * mm))
    elements.append(Paragraph('Assignment Submission Summary (Subject-wise)', section_style))
    assignment_header = ['Subject', 'Session', 'Sem', 'Total', 'Submitted', 'Pending', 'Submission %']
    assignment_data = [assignment_header]
    for row in subject_summary_rows:
        assignment_data.append([
            row['subject'],
            row['session'],
            str(row['semester']),
            str(row['assignment_total']),
            str(row['assignment_submitted']),
            str(row['assignment_pending']),
            str(row['assignment_submission_pct']) if row['assignment_submission_pct'] is not None else 'N/A',
        ])

    assignment_col_widths = [50 * mm, 40 * mm, 14 * mm, 20 * mm, 20 * mm, 20 * mm, 24 * mm]
    assignment_table = Table(assignment_data, repeatRows=1, colWidths=assignment_col_widths)
    assignment_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#334155')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#9ca3af')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f9fafb'), colors.HexColor('#f3f4f6')]),
        ('ALIGN', (2, 1), (-1, -1), 'CENTER'),
    ]))
    elements.append(assignment_table)

    elements.append(Spacer(1, 4 * mm))
    elements.append(Paragraph('Exam-wise Marks', section_style))
    exam_header = ['Subject', 'Session', 'Sem', 'Exam', 'Date', 'Time', 'Marks', 'Total']
    exam_data = [exam_header]
    for e in exam_detail_rows:
        exam_data.append([
            e['subject'], e['session'], str(e['semester']), e['exam_type'],
            e['exam_date'], e['exam_time'], e['marks_text'], str(e['total_marks'])
        ])

    exam_col_widths = [28 * mm, 22 * mm, 10 * mm, 22 * mm, 20 * mm, 18 * mm, 12 * mm, 12 * mm]
    exam_table = Table(exam_data, repeatRows=1, colWidths=exam_col_widths)
    exam_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f2937')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 7.5),
        ('GRID', (0, 0), (-1, -1), 0.25, colors.HexColor('#9ca3af')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.whitesmoke, colors.lightgrey]),
        ('ALIGN', (2, 1), (-1, -1), 'CENTER'),
    ]))
    elements.append(exam_table)

    def _draw_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#6b7280'))
        canvas.drawString(12 * mm, 7 * mm, f"Generated by Admin on {generated_at}")
        canvas.drawRightString(198 * mm, 7 * mm, f"Page {canvas.getPageNumber()}")

        canvas.setFont('Helvetica-Bold', 34)
        canvas.setFillColor(colors.HexColor('#e5e7eb'))
        canvas.saveState()
        canvas.translate(105 * mm, 145 * mm)
        canvas.rotate(35)
        canvas.drawCentredString(0, 0, 'INTERNAL ACADEMIC REPORT')
        canvas.restoreState()
        canvas.restoreState()

    doc.build(elements, onFirstPage=_draw_footer, onLaterPages=_draw_footer)
    pdf = buffer.getvalue()
    buffer.close()

    safe_student = slugify(student.name) or 'student'
    filename = f"admin-student-report-{safe_student}.pdf"
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_GET
def student_my_report_pdf(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can download this report.')

    return admin_student_report_pdf(request, request.user.student.id)

@require_GET
@login_required
def admin_teacher_report_pdf(request, teacher_id):
    if not (request.user.is_superuser or request.user.is_staff):
        return HttpResponseForbidden('Only admins can download this report.')

    teacher = get_object_or_404(Teacher.objects.select_related('dept', 'course', 'user'), id=teacher_id)
    teacher_subjects = Subject.objects.filter(teachers=teacher).select_related('session').order_by('session__name', 'semester', 'name')

    now_local = timezone.localtime()
    month_classes = AttendanceRecord.objects.filter(
        marked_by=teacher,
        session_held=True,
        date__year=now_local.year,
        date__month=now_local.month,
    ).count()
    total_classes = AttendanceRecord.objects.filter(marked_by=teacher, session_held=True).count()

    exams_created = Exam.objects.filter(created_by=teacher).count()
    exams_conducted = Exam.objects.filter(created_by=teacher, status='Conducted').count()

    conducted_subject_exams = list(
        Exam.objects.filter(subject__in=teacher_subjects, status='Conducted').select_related('subject', 'session')
    )
    entered_marks = ExamMark.objects.filter(exam__in=conducted_subject_exams).count()
    expected_marks = sum(exam.subject.student_set.count() for exam in conducted_subject_exams)
    marks_completion_pct = round((entered_marks / expected_marks) * 100, 2) if expected_marks else None

    generated_at = now_local.strftime('%Y-%m-%d %I:%M %p')
    last_login = timezone.localtime(teacher.user.last_login).strftime('%Y-%m-%d %I:%M %p') if teacher.user and teacher.user.last_login else 'N/A'

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )
    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'AdminTeacherTitle',
        parent=styles['Title'],
        fontName='Helvetica-Bold',
        fontSize=20,
        alignment=TA_CENTER,
        textColor=colors.HexColor('#0f172a'),
        spaceAfter=8,
    )
    section_style = ParagraphStyle(
        'AdminTeacherSection',
        parent=styles['Heading4'],
        fontName='Helvetica-Bold',
        fontSize=11,
        textColor=colors.HexColor('#1d4ed8'),
        spaceBefore=4,
        spaceAfter=4,
    )
    normal_small = ParagraphStyle(
        'AdminTeacherNormalSmall',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#111827'),
    )

    elements = []
    elements.append(Paragraph('Teacher Academic Snapshot', title_style))
    elements.append(Paragraph(
        f"{teacher.name} | {teacher.dept.name if teacher.dept else '-'} | {teacher.course.name if teacher.course else '-'}",
        ParagraphStyle(
            'AdminTeacherSubtitle',
            parent=normal_small,
            alignment=TA_CENTER,
            textColor=colors.HexColor('#4b5563'),
            spaceAfter=2,
        )
    ))
    elements.append(Paragraph(
        f"Generated At: {generated_at}",
        ParagraphStyle(
            'AdminTeacherGeneratedAt',
            parent=normal_small,
            alignment=TA_CENTER,
            textColor=colors.HexColor('#4b5563'),
            spaceAfter=6,
        )
    ))

    photo_cell = Paragraph('No Photo', normal_small)
    if teacher.photo and hasattr(teacher.photo, 'path'):
        try:
            photo_cell = Image(teacher.photo.path, width=30 * mm, height=34 * mm)
        except Exception:
            pass

    profile_table = Table([
        [Paragraph('<b>Teacher Name</b>', normal_small), Paragraph(teacher.name, normal_small)],
        [Paragraph('<b>Email</b>', normal_small), Paragraph(teacher.email or '-', normal_small)],
        [Paragraph('<b>Phone</b>', normal_small), Paragraph(teacher.phone or '-', normal_small)],
        [Paragraph('<b>Department</b>', normal_small), Paragraph(str(teacher.dept.name if teacher.dept else '-'), normal_small)],
        [Paragraph('<b>Course</b>', normal_small), Paragraph(str(teacher.course.name if teacher.course else '-'), normal_small)],
        [Paragraph('<b>Status</b>', normal_small), Paragraph(teacher.status or '-', normal_small)],
        [Paragraph('<b>Last Login</b>', normal_small), Paragraph(last_login, normal_small)],
    ], colWidths=[42 * mm, 106 * mm])
    profile_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#eff6ff')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#1e3a8a')),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#cbd5e1')),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))

    head_table = Table([[photo_cell, profile_table]], colWidths=[33 * mm, 153 * mm])
    head_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (0, 0), (0, 0), 'CENTER'),
        ('BOX', (0, 0), (-1, -1), 0.6, colors.HexColor('#cbd5e1')),
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f8fafc')),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
    ]))
    elements.append(head_table)
    elements.append(Spacer(1, 4 * mm))

    elements.append(Paragraph('Performance Snapshot', section_style))
    metrics_data = [
        ['Assigned Subjects', str(teacher_subjects.count())],
        ['Classes This Month', str(month_classes)],
        ['Classes Marked (Total)', str(total_classes)],
        ['Exams Created / Conducted', f"{exams_created} / {exams_conducted}"],
        ['Marks Completion', f"{marks_completion_pct if marks_completion_pct is not None else 'N/A'}% ({entered_marks}/{expected_marks})"],
    ]
    metrics_table = Table(metrics_data, colWidths=[58 * mm, 128 * mm])
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (0, -1), colors.HexColor('#fef3c7')),
        ('TEXTCOLOR', (0, 0), (0, -1), colors.HexColor('#92400e')),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#d1d5db')),
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    elements.append(metrics_table)
    elements.append(Spacer(1, 4 * mm))

    elements.append(Paragraph('Assigned Subjects', section_style))
    subject_data = [['Session', 'Semester', 'Subject', 'Code']]
    for subject in teacher_subjects:
        subject_data.append([
            subject.session.name if subject.session else '-',
            str(subject.semester),
            subject.name,
            subject.code or '-',
        ])
    if len(subject_data) == 1:
        subject_data.append(['-', '-', 'No assigned subjects', '-'])

    subject_table = Table(subject_data, repeatRows=1, colWidths=[34 * mm, 22 * mm, 96 * mm, 34 * mm])
    subject_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#0f766e')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('GRID', (0, 0), (-1, -1), 0.35, colors.HexColor('#9ca3af')),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#f9fafb'), colors.HexColor('#f3f4f6')]),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('ALIGN', (1, 1), (1, -1), 'CENTER'),
    ]))
    elements.append(subject_table)

    def draw_footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont('Helvetica', 8)
        canvas.setFillColor(colors.HexColor('#6b7280'))
        canvas.drawString(12 * mm, 7 * mm, f"Generated by Admin on {generated_at}")
        canvas.drawRightString(198 * mm, 7 * mm, f"Page {canvas.getPageNumber()}")

        canvas.setFont('Helvetica-Bold', 34)
        canvas.setFillColor(colors.HexColor('#e5e7eb'))
        canvas.saveState()
        canvas.translate(105 * mm, 145 * mm)
        canvas.rotate(35)
        canvas.drawCentredString(0, 0, 'INTERNAL ACADEMIC REPORT')
        canvas.restoreState()
        canvas.restoreState()

    doc.build(elements, onFirstPage=draw_footer, onLaterPages=draw_footer)
    pdf = buffer.getvalue()
    buffer.close()

    safe_teacher = slugify(teacher.name) or 'teacher'
    filename = f"admin-teacher-report-{safe_teacher}.pdf"
    response = HttpResponse(pdf, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{filename}"'
    return response


@login_required
@require_POST
def admin_deactivate_teacher_login(request, teacher_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Only admins can deactivate logins.')

    teacher = get_object_or_404(Teacher, id=teacher_id)
    if not teacher.user:
        messages.warning(request, f"No login account linked for teacher {teacher.name}.")
        return redirect('pending_requests_list')

    if not teacher.user.is_active:
        messages.info(request, f"Login already disabled for teacher {teacher.name}.")
        return redirect('pending_requests_list')

    teacher.user.is_active = False
    teacher.user.save(update_fields=['is_active'])
    messages.success(request, f"Login disabled for teacher {teacher.name}.")
    return redirect('pending_requests_list')


@login_required
@require_POST
def admin_reactivate_teacher_login(request, teacher_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Only admins can reactivate logins.')

    teacher = get_object_or_404(Teacher, id=teacher_id)
    if not teacher.user:
        messages.warning(request, f"No login account linked for teacher {teacher.name}.")
        return redirect('pending_requests_list')

    if teacher.user.is_active:
        messages.info(request, f"Login already active for teacher {teacher.name}.")
        return redirect('pending_requests_list')

    teacher.user.is_active = True
    teacher.user.save(update_fields=['is_active'])
    messages.success(request, f"Login enabled for teacher {teacher.name}.")
    return redirect('pending_requests_list')


@login_required
@require_POST
def admin_deactivate_student_login(request, student_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Only admins can deactivate logins.')

    student = get_object_or_404(Student, id=student_id)
    if not student.user:
        messages.warning(request, f"No login account linked for student {student.name}.")
        return redirect('pending_requests_list')

    if not student.user.is_active:
        messages.info(request, f"Login already disabled for student {student.name}.")
        return redirect('pending_requests_list')

    student.user.is_active = False
    student.user.save(update_fields=['is_active'])
    messages.success(request, f"Login disabled for student {student.name}.")
    return redirect('pending_requests_list')


@login_required
@require_POST
def admin_reactivate_student_login(request, student_id):
    if not request.user.is_superuser:
        return HttpResponseForbidden('Only admins can reactivate logins.')

    student = get_object_or_404(Student, id=student_id)
    if not student.user:
        messages.warning(request, f"No login account linked for student {student.name}.")
        return redirect('pending_requests_list')

    if student.user.is_active:
        messages.info(request, f"Login already active for student {student.name}.")
        return redirect('pending_requests_list')

    student.user.is_active = True
    student.user.save(update_fields=['is_active'])
    messages.success(request, f"Login enabled for student {student.name}.")
    return redirect('pending_requests_list')


@require_GET
def search_teacher(request):
    email = request.GET.get('email')
    if not email:
        return JsonResponse({'error': 'Email is required.'}, status=400)
    try:
        teacher = Teacher.objects.get(email=email)
        # Query all subjects where this teacher is assigned
        subjects = Subject.objects.filter(teachers=teacher)
        subject_names = [sub.name for sub in subjects]
        hod_assignments = HODAssignment.objects.filter(
            teacher=teacher,
            is_active=True,
        ).select_related('course', 'department')
        hod_assignment_labels = [
            f"{assignment.department.name} - Year {assignment.year} ({assignment.course.name})"
            for assignment in hod_assignments
        ]

        data = {
            'id': teacher.id,
            'name': teacher.name,
            'email': teacher.email,
            'phone': teacher.phone,
            'department': teacher.dept.name if teacher.dept else '',
            'course': teacher.course.name if teacher.course else '',
            'sex': teacher.sex,
            'dob': teacher.dob.strftime('%b %d, %Y') if teacher.dob else '',
            'photo_url': teacher.photo.url if teacher.photo else '',
            'status': teacher.status,
            'status_class': 'status-active' if teacher.status == 'Active' else ('status-pending' if teacher.status == 'Pending' else 'status-other'),
            'login_active': bool(teacher.user and teacher.user.is_active),
            'subjects': subject_names,
            'hod_assignments': hod_assignment_labels,
        }
        subject_count = subjects.count()
        now_local = timezone.localtime()
        month_classes = AttendanceRecord.objects.filter(
            marked_by=teacher,
            session_held=True,
            date__year=now_local.year,
            date__month=now_local.month,
        ).count()
        total_classes = AttendanceRecord.objects.filter(marked_by=teacher, session_held=True).count()
        exams_created = Exam.objects.filter(created_by=teacher).count()
        exams_conducted = Exam.objects.filter(created_by=teacher, status='Conducted').count()
        conducted_subject_exams = list(
            Exam.objects.filter(subject__in=subjects, status='Conducted').select_related('subject')
        )
        entered_marks = ExamMark.objects.filter(exam__in=conducted_subject_exams).count()
        expected_marks = sum(exam.subject.student_set.count() for exam in conducted_subject_exams)
        completion_pct = round((entered_marks / expected_marks) * 100, 2) if expected_marks else None

        data['snapshot'] = {
            'assigned_subjects': subject_count,
            'classes_this_month': month_classes,
            'classes_marked_total': total_classes,
            'exams_created': exams_created,
            'exams_conducted': exams_conducted,
            'marks_completion_text': f"{completion_pct}% ({entered_marks}/{expected_marks})" if completion_pct is not None else 'N/A',
        }
        if request.user.is_authenticated and (request.user.is_superuser or request.user.is_staff):
            data['report_pdf_url'] = reverse('admin_teacher_report_pdf', args=(teacher.id,))
            if data['login_active']:
                data['deactivate_url'] = reverse('admin_deactivate_teacher_login', args=(teacher.id,))
            else:
                data['reactivate_url'] = reverse('admin_reactivate_teacher_login', args=(teacher.id,))
        return JsonResponse(data)
    except Teacher.DoesNotExist:
        return JsonResponse({'error': 'Teacher not found.'}, status=404)



@require_GET
def search_student(request):
    roll = request.GET.get('roll')
    if not roll:
        return JsonResponse({'error': 'Roll number is required.'}, status=400)
    try:
        student = Student.objects.get(roll=roll)
        data = {
            'id': student.id,
            'photo_url': student.photo.url if student.photo else '',
            'name': student.name,
            'roll': student.roll,
            'email': student.email,
            'department': student.dept.name if student.dept else '',
            'course': student.course.name if student.course else '',
            'sex': student.sex,
            'dob': student.dob.strftime('%b %d, %Y') if student.dob else '',
            'status': student.status,
            'login_active': bool(student.user and student.user.is_active),
        }
        if request.user.is_authenticated and (request.user.is_superuser or request.user.is_staff):
            data['report_pdf_url'] = reverse('admin_student_report_pdf', args=(student.id,))
            if data['login_active']:
                data['deactivate_url'] = reverse('admin_deactivate_student_login', args=(student.id,))
            else:
                data['reactivate_url'] = reverse('admin_reactivate_student_login', args=(student.id,))
        return JsonResponse(data)
    except Student.DoesNotExist:
        return JsonResponse({'error': 'Student not found.'}, status=404)


# Exam Management Views

@login_required
@never_cache
def exam_list(request):
    """List all scheduled exams."""
    if not hasattr(request.user, 'teacher'):
        messages.error(request, 'Only teachers can manage exams.')
        return redirect('home')
    
    teacher = request.user.teacher
    
    # Get filter parameters
    session_id = request.GET.get('session')
    semester = request.GET.get('semester')
    exam_type = request.GET.get('exam_type')
    status = request.GET.get('status')
    
    # Show all exams (not restricted to teacher-assigned subjects).
    exams = Exam.objects.select_related('subject', 'session', 'created_by')
    
    # Apply filters
    if session_id:
        exams = exams.filter(session_id=session_id)
    if semester:
        exams = exams.filter(semester=semester)
    if exam_type:
        exams = exams.filter(exam_type=exam_type)
    if status:
        exams = exams.filter(status=status)
    
    # Get unique sessions and semesters for filter dropdowns
    sessions = Session.objects.all().order_by('-name')
    semesters = Exam.objects.values_list('semester', flat=True).distinct().order_by('semester')
    exam_types = Exam.EXAM_TYPE_CHOICES
    statuses = Exam.STATUS_CHOICES
    
    exams = exams.order_by('-created_at')
    academic_exam_types = ['Sessional 1', 'Sessional 2', 'Mid Term']
    academic_exams = exams.filter(exam_type__in=academic_exam_types)
    practical_exams = exams.filter(exam_type='Internal Practical')
    
    context = {
        'exams': exams,
        'academic_exams': academic_exams,
        'practical_exams': practical_exams,
        'sessions': sessions,
        'semesters': semesters,
        'exam_types': exam_types,
        'statuses': statuses,
        'selected_session': session_id,
        'selected_semester': semester,
        'selected_exam_type': exam_type,
        'selected_status': status,
    }
    
    return render(request, 'info/exam_list.html', context)


@login_required
@never_cache
def exam_create(request):
    """Create a new exam."""
    if not hasattr(request.user, 'teacher'):
        messages.error(request, 'Only teachers can create exams.')
        return redirect('home')
    
    teacher = request.user.teacher
    
    if request.method == 'POST':
        subject_id = request.POST.get('subject')
        exam_type = request.POST.get('exam_type')
        session_id = request.POST.get('session')
        semester = request.POST.get('semester')
        date_conducted = request.POST.get('date_conducted')
        exam_time = request.POST.get('exam_time')
        total_marks = request.POST.get('total_marks')
        status = 'Pending'  # New exams are always pending
        
        # Get the subject (no teacher restriction)
        subject = get_object_or_404(Subject, id=subject_id)
        session_obj = get_object_or_404(Session, id=session_id)
        try:
            semester_int = int(semester)
        except (TypeError, ValueError):
            messages.error(request, 'Invalid semester selected.')
            return redirect('exam_create')

        # Strict validation for flow: selected subject must belong to selected session + semester.
        if subject.session_id != session_obj.id or int(subject.semester) != semester_int:
            messages.error(request, 'Subject does not match the selected session and semester.')
            return redirect('exam_create')
        
        # Check if exam already exists
        existing = Exam.objects.filter(
            subject=subject,
            exam_type=exam_type,
            session=session_obj,
            semester=semester_int
        ).first()
        
        if existing:
            messages.warning(request, f'Exam {exam_type} already exists for this subject and session.')
            return redirect('exam_list')
        
        # Create or update exam
        exam, created = Exam.objects.get_or_create(
            subject=subject,
            exam_type=exam_type,
            session=session_obj,
            semester=semester_int,
            defaults={
                'date_conducted': date_conducted if date_conducted else None,
                'exam_time': exam_time if exam_time else None,
                'total_marks': int(total_marks) if total_marks else 25,
                'status': status,
                'created_by': teacher,
            }
        )
        
        if created:
            messages.success(request, f'Exam {exam_type} created successfully.')
        else:
            exam.date_conducted = date_conducted if date_conducted else None
            exam.exam_time = exam_time if exam_time else None
            exam.total_marks = int(total_marks) if total_marks else 25
            exam.status = status
            exam.save()
            messages.success(request, f'Exam {exam_type} updated successfully.')

        # Auto-schedule this exam for students in selected session+semester for this subject.
        eligible_students = Student.objects.filter(
            current_session=session_obj,
            current_semester=semester_int,
            subjects=subject,
        ).exclude(status='Rejected').distinct()
        exam.scheduled_students.set(eligible_students)
        messages.info(request, f'Exam auto-scheduled for {eligible_students.count()} students.')
        
        return redirect('exam_list')
    
    # GET request - show form
    # Fetch ALL subjects (not just assigned to teacher) for exam creation
    all_subjects = Subject.objects.select_related('session').order_by('semester', 'name')
    sessions = Session.objects.all().order_by('-name')
    exam_types = Exam.EXAM_TYPE_CHOICES
    all_subjects_data = [
        {
            'id': subject.id,
            'name': subject.name,
            'session_id': subject.session_id,
            'semester': subject.semester,
            'session_name': subject.session.name,
        }
        for subject in all_subjects
    ]
    
    context = {
        'teacher_subjects': all_subjects,
        'sessions': sessions,
        'exam_types': exam_types,
        'all_subjects_json': json.dumps(all_subjects_data),
    }
    
    return render(request, 'info/exam_create.html', context)


@login_required
def exam_mark_conducted(request, exam_id):
    """Mark an exam as conducted."""
    exam = get_object_or_404(Exam, id=exam_id)
    
    if not hasattr(request.user, 'teacher') or exam.created_by != request.user.teacher:
        messages.error(request, 'You can only mark your own exams as conducted.')
        return redirect('exam_list')
    
    if request.method == 'POST':
        # Keep pre-scheduled date/time and only set a fallback date if missing.
        if not exam.date_conducted:
            exam.date_conducted = date.today()

        exam.status = 'Conducted'
        exam.save(update_fields=['date_conducted', 'status'])
        messages.success(request, f'Exam marked as conducted on {exam.date_conducted}.')
    
    return redirect('exam_list')


@login_required
def exam_delete(request, exam_id):
    """Delete an exam."""
    exam = get_object_or_404(Exam, id=exam_id)
    
    if not hasattr(request.user, 'teacher') or exam.created_by != request.user.teacher:
        messages.error(request, 'You can only delete your own exams.')
        return redirect('exam_list')
    
    if request.method == 'POST':
        exam_name = f"{exam.exam_type} ({exam.subject.name})"
        exam.delete()
        messages.success(request, f'Exam {exam_name} deleted successfully.')
        return redirect('exam_list')
    
    return render(request, 'info/exam_confirm_delete.html', {'exam': exam})


@login_required
def teacher_assign_marks(request, assign_id):
    """Open the modern exam-mark workflow for one of the teacher's assigned classes."""
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can enter marks.')

    assignment = get_object_or_404(
        Assign.objects.select_related('session', 'course', 'teacher'),
        id=assign_id,
        teacher=request.user.teacher,
    )
    filters = {'course': assignment.course_id}
    if assignment.session_id:
        filters['session'] = assignment.session_id
    query = urlencode(filters)
    return redirect(f"{reverse('exam_marks_list')}?{query}")


@login_required
def exam_marks_list(request):
    """List conducted exams available for marks entry for assigned teachers."""
    if not hasattr(request.user, 'teacher'):
        messages.error(request, 'Only teachers can enter marks.')
        return redirect('home')

    teacher = request.user.teacher
    session_id = request.GET.get('session')
    semester = request.GET.get('semester')
    exam_type = request.GET.get('exam_type')
    course_id = request.GET.get('course')

    try:
        session_id = int(session_id) if session_id else None
        semester = int(semester) if semester else None
        course_id = int(course_id) if course_id else None
    except (TypeError, ValueError):
        messages.error(request, 'Invalid exam filter. Please select valid session, semester and course values.')
        return redirect('exam_marks_list')
    if semester is not None and not 1 <= semester <= 8:
        messages.error(request, 'Invalid semester filter.')
        return redirect('exam_marks_list')

    exams = Exam.objects.filter(
        status='Conducted',
        subject__teachers=teacher,
    ).select_related('subject', 'session', 'subject__course').prefetch_related('scheduled_students', 'marks_entries').distinct()

    if session_id:
        exams = exams.filter(session_id=session_id)
    if course_id:
        exams = exams.filter(subject__course_id=course_id)
    if semester:
        exams = exams.filter(semester=semester)
    if exam_type:
        exams = exams.filter(exam_type=exam_type)

    exams = exams.order_by('-date_conducted', '-created_at')

    exam_rows = []
    for exam in exams:
        scheduled_count = exam.scheduled_students.count()
        entered_count = exam.marks_entries.count()
        exam_rows.append({
            'exam': exam,
            'scheduled_count': scheduled_count,
            'entered_count': entered_count,
            'remaining_count': max(0, scheduled_count - entered_count),
        })

    context = {
        'exam_rows': exam_rows,
        'sessions': Session.objects.all().order_by('-name'),
        'semesters': exams.values_list('semester', flat=True).distinct().order_by('semester'),
        'exam_types': Exam.EXAM_TYPE_CHOICES,
        'selected_session': session_id,
        'selected_semester': semester,
        'selected_exam_type': exam_type,
        'selected_course': course_id,
    }
    return render(request, 'info/exam_marks_list.html', context)


@login_required
def exam_marks_entry(request, exam_id):
    """Enter/update marks for a conducted exam."""
    if not hasattr(request.user, 'teacher'):
        messages.error(request, 'Only teachers can enter marks.')
        return redirect('home')

    teacher = request.user.teacher
    exam = get_object_or_404(
        Exam.objects.select_related('subject', 'session'),
        id=exam_id,
        status='Conducted',
    )

    if not exam.subject.teachers.filter(id=teacher.id).exists():
        messages.error(request, 'You can enter marks only for subjects assigned to you.')
        return redirect('exam_marks_list')

    students = exam.scheduled_students.exclude(status='Rejected').order_by('roll', 'name')
    marks_map = {
        mark.student_id: mark
        for mark in ExamMark.objects.filter(exam=exam).select_related('student')
    }

    if request.method == 'POST':
        has_error = False
        for student in students:
            is_absent = bool(request.POST.get(f'absent_{student.id}'))
            marks_raw = (request.POST.get(f'marks_{student.id}') or '').strip()

            if is_absent:
                ExamMark.objects.update_or_create(
                    exam=exam,
                    student=student,
                    defaults={
                        'marks_obtained': None,
                        'is_absent': True,
                        'entered_by': teacher,
                    },
                )
                continue

            if not marks_raw:
                messages.error(request, f'Enter marks or mark absent for {student.name}.')
                has_error = True
                continue

            try:
                marks_value = int(marks_raw)
            except ValueError:
                messages.error(request, f'Invalid marks for {student.name}.')
                has_error = True
                continue

            if marks_value < 0 or marks_value > exam.total_marks:
                messages.error(request, f'Marks for {student.name} must be between 0 and {exam.total_marks}.')
                has_error = True
                continue

            ExamMark.objects.update_or_create(
                exam=exam,
                student=student,
                defaults={
                    'marks_obtained': marks_value,
                    'is_absent': False,
                    'entered_by': teacher,
                },
            )

        if has_error:
            return redirect('exam_marks_entry', exam_id=exam.id)

        messages.success(request, 'Marks saved successfully.')
        return redirect('exam_marks_list')

    context = {
        'exam': exam,
        'students': students,
        'marks_map': marks_map,
    }
    return render(request, 'info/exam_marks_entry.html', context)


@require_POST
def set_theme_mode(request):
    requested_mode = (request.POST.get('theme_mode') or '').strip().lower()
    if requested_mode not in {'light', 'dark'}:
        requested_mode = 'light'

    request.session['theme_mode'] = requested_mode

    next_url = (request.POST.get('next') or '').strip()
    if not next_url or not url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        next_url = reverse('home')

    return redirect(next_url)


@login_required
@user_passes_test(is_superuser)
@require_POST
def approve_all_teachers(request):
    approved_count = 0
    failed_count = 0
    for pending in list(PendingTeacher.objects.all()):
        try:
            teacher, email = _approve_teacher_request_record(pending)
            notify_registration_decision(
                request, email, teacher.name, 'Teacher Registration Approved',
                f'Hello {teacher.name}, your teacher registration has been approved.',
            )
            approved_count += 1
        except (ValidationError, IntegrityError, ValueError):
            failed_count += 1

    if approved_count:
        messages.success(request, f'Approved {approved_count} teacher request(s).')
    if failed_count:
        messages.warning(request, f'{failed_count} teacher request(s) could not be approved. Review their course, department, duplicate email and account data.')
    if not approved_count and not failed_count:
        messages.info(request, 'No pending teacher requests found.')
    return redirect('pending_requests_list')


@login_required
@user_passes_test(is_superuser)
@require_POST
def approve_all_students(request):
    approved_count = 0
    failed_count = 0
    for pending in list(PendingStudent.objects.all()):
        try:
            student, _assigned_count, email = _approve_student_request_record(pending, request.user)
            notify_registration_decision(
                request, email, student.name, 'Student Registration Approved',
                f'Hello {student.name}, your registration has been approved. Your current semester is {student.current_semester}.',
            )
            approved_count += 1
        except (ValidationError, IntegrityError, ValueError):
            failed_count += 1

    if approved_count:
        messages.success(request, f'Approved {approved_count} student request(s).')
    if failed_count:
        messages.warning(request, f'{failed_count} student request(s) could not be approved. Review their records for duplicate, missing, or invalid academic data.')
    if not approved_count and not failed_count:
        messages.info(request, 'No pending student requests found.')
    return redirect('pending_requests_list')


@login_required
def student_document_vault(request):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can access document vault.')

    student = request.user.student
    share_teacher_choices = Teacher.objects.filter(status__iexact='approved')
    if student.course_id:
        share_teacher_choices = share_teacher_choices.filter(course=student.course)
    if student.dept_id:
        share_teacher_choices = share_teacher_choices.filter(dept=student.dept)
    share_teacher_choices = share_teacher_choices.select_related('dept', 'course').order_by('name')

    if request.method == 'POST':
        action = (request.POST.get('action') or 'upload').strip().lower()

        if action == 'share':
            document_id = request.POST.get('document_id')
            teacher_id = request.POST.get('teacher_id')
            share_note = (request.POST.get('share_note') or '').strip()

            document = get_object_or_404(StudentDocument, id=document_id, student=student)
            if document.status != StudentDocument.STATUS_VERIFIED:
                messages.error(request, 'Only verified documents can be shared.')
                return redirect('student_document_vault')

            teacher_filter = Teacher.objects.filter(status__iexact='approved')
            if student.course_id:
                teacher_filter = teacher_filter.filter(course=student.course)
            if student.dept_id:
                teacher_filter = teacher_filter.filter(dept=student.dept)

            try:
                teacher = teacher_filter.get(id=teacher_id)
            except (Teacher.DoesNotExist, ValueError):
                messages.error(request, 'Please choose a valid teacher to share with.')
                return redirect('student_document_vault')

            StudentDocumentShare.objects.update_or_create(
                student_document=document,
                teacher=teacher,
                defaults={
                    'share_note': share_note,
                    'is_active': True,
                },
            )
            messages.success(request, f'Document shared with {teacher.name}.')
            return redirect('student_document_vault')

        document_type = (request.POST.get('document_type') or StudentDocument.DOC_OTHER).strip()
        title = (request.POST.get('title') or '').strip()
        document_file = request.FILES.get('document_file')

        valid_types = {choice[0] for choice in StudentDocument.DOCUMENT_TYPE_CHOICES}
        if document_type not in valid_types:
            document_type = StudentDocument.DOC_OTHER

        if not title or not document_file:
            messages.error(request, 'Document title and file are required.')
            return redirect('student_document_vault')

        file_name = document_file.name.lower()
        allowed_extensions = ('.pdf', '.jpg', '.jpeg', '.png', '.webp')
        if not file_name.endswith(allowed_extensions):
            messages.error(request, 'Only PDF, JPG, JPEG, PNG, and WEBP files are allowed.')
            return redirect('student_document_vault')

        max_file_size = 10 * 1024 * 1024
        if document_file.size > max_file_size:
            messages.error(request, 'Document size should be 10 MB or less.')
            return redirect('student_document_vault')

        StudentDocument.objects.create(
            student=student,
            document_type=document_type,
            title=title,
            document_file=document_file,
            status=StudentDocument.STATUS_PENDING,
        )
        messages.success(request, 'Document uploaded successfully.')
        return redirect('student_document_vault')

    documents = StudentDocument.objects.filter(student=student).select_related('reviewed_by')
    documents = documents.prefetch_related('shares__teacher')
    return render(request, 'info/student_document_vault.html', {
        'documents': documents,
        'document_type_choices': StudentDocument.DOCUMENT_TYPE_CHOICES,
        'share_teacher_choices': share_teacher_choices,
    })


@login_required
@require_POST
def student_document_delete(request, document_id):
    if not hasattr(request.user, 'student'):
        return HttpResponseForbidden('Only students can delete documents.')

    document = get_object_or_404(StudentDocument, id=document_id, student=request.user.student)
    if document.document_file:
        document.document_file.delete(save=False)
    document.delete()
    messages.success(request, 'Document deleted.')
    return redirect('student_document_vault')


@login_required
@require_GET
def student_document_download(request, document_id):
    document = get_object_or_404(StudentDocument.objects.select_related('student'), id=document_id)

    is_owner = hasattr(request.user, 'student') and request.user.student.id == document.student_id
    is_admin = request.user.is_superuser or request.user.is_staff
    is_shared_teacher = hasattr(request.user, 'teacher') and StudentDocumentShare.objects.filter(
        student_document=document,
        teacher=request.user.teacher,
        is_active=True,
    ).exists()
    if not (is_owner or is_admin or is_shared_teacher):
        return HttpResponseForbidden('You are not allowed to access this document.')

    if not document.document_file:
        raise Http404('Document file not found.')

    try:
        return FileResponse(document.document_file.open('rb'), as_attachment=True, filename=document.document_file.name.split('/')[-1])
    except FileNotFoundError:
        raise Http404('Document file not found.')


@login_required
def admin_student_document_vault(request):
    if not (request.user.is_superuser or request.user.is_staff):
        return HttpResponseForbidden('Only admins can access document review.')

    selected_status = (request.GET.get('status') or 'All').strip()
    valid_status = {'All', StudentDocument.STATUS_PENDING, StudentDocument.STATUS_VERIFIED, StudentDocument.STATUS_REJECTED}
    if selected_status not in valid_status:
        selected_status = 'All'

    if request.method == 'POST':
        action = (request.POST.get('action') or '').strip().lower()
        document_id = request.POST.get('document_id')
        review_note = (request.POST.get('review_note') or '').strip()

        document = get_object_or_404(StudentDocument, id=document_id)
        if action not in {'verify', 'reject'}:
            messages.error(request, 'Invalid document action.')
            return redirect('admin_student_document_vault')

        document.status = StudentDocument.STATUS_VERIFIED if action == 'verify' else StudentDocument.STATUS_REJECTED
        document.review_note = review_note
        document.reviewed_by = request.user
        document.reviewed_at = timezone.now()
        document.save(update_fields=['status', 'review_note', 'reviewed_by', 'reviewed_at', 'updated_at'])
        messages.success(request, f'Document {document.title} updated successfully.')
        return redirect('admin_student_document_vault')

    documents = StudentDocument.objects.select_related('student', 'student__dept', 'student__course', 'reviewed_by')
    if selected_status != 'All':
        documents = documents.filter(status=selected_status)
    documents = documents.order_by('-uploaded_at')

    return render(request, 'info/admin_student_document_vault.html', {
        'documents': documents,
        'selected_status': selected_status,
        'status_choices': ['All', StudentDocument.STATUS_PENDING, StudentDocument.STATUS_VERIFIED, StudentDocument.STATUS_REJECTED],
    })


@login_required
def teacher_shared_documents(request):
    if not hasattr(request.user, 'teacher'):
        return HttpResponseForbidden('Only teachers can access shared documents.')

    teacher = request.user.teacher
    shared_items = StudentDocumentShare.objects.filter(
        teacher=teacher,
        is_active=True,
    ).select_related(
        'student_document',
        'student_document__student',
        'student_document__reviewed_by',
    ).order_by('-shared_at')

    return render(request, 'info/teacher_shared_documents.html', {
        'shared_items': shared_items,
    })

