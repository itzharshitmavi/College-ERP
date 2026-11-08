from django.shortcuts import render
from info.models import *
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.pagination import PageNumberPagination
from itertools import chain
from rest_framework import serializers, status
from rest_framework.generics import ListAPIView
from django.db.models.signals import post_save
from rest_framework.generics import get_object_or_404
from rest_framework import generics
from rest_framework import mixins
from rest_framework import status
from django.db.models import Sum, Count
from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.db import transaction
from django.utils import timezone
import math
import hashlib
import apis.serializers as api_ser


class DetailView(APIView):
    """Return the authenticated student's profile."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not hasattr(request.user, 'student'):
            return Response({'message': 'Only students can access this endpoint.'}, status=status.HTTP_403_FORBIDDEN)
        serializer = api_ser.DetailSerializer(request.user.student, context={'request': request})
        return Response({'data': serializer.data}, status=status.HTTP_200_OK)


class AttendanceView(APIView):
    """Return attendance totals grouped by subject for the authenticated student."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not hasattr(request.user, 'student'):
            return Response({'message': 'Only students can access this endpoint.'}, status=status.HTTP_403_FORBIDDEN)
        student = request.user.student
        rows = StudentAttendance.objects.filter(
            student=student,
            attendance_record__date__lte=timezone.localdate(),
        ).select_related(
            'attendance_record',
            'attendance_record__timetable_slot',
            'attendance_record__timetable_slot__subject',
        )
        by_subject = {}
        for row in rows:
            subject = row.attendance_record.timetable_slot.subject
            key = subject.pk
            if key not in by_subject:
                by_subject[key] = {
                    'subject_id': subject.pk,
                    'subject_name': subject.name,
                    'subject_code': subject.code or '',
                    'total_classes': 0,
                    'classes_attended': 0,
                    'classes_absent': 0,
                }
            by_subject[key]['total_classes'] += 1
            if row.status:
                by_subject[key]['classes_attended'] += 1
            else:
                by_subject[key]['classes_absent'] += 1

        payload = []
        for item in by_subject.values():
            total = item['total_classes']
            attended = item['classes_attended']
            item['attendance_percentage'] = round(attended / total * 100, 2) if total else 0.0
            item['classes_to_attend'] = max(0, math.ceil((0.75 * total - attended) / 0.25)) if total else 0
            payload.append(item)
        serializer = api_ser.StudentAttendanceSummarySerializer(payload, many=True)
        return Response({'user_attendance': serializer.data}, status=status.HTTP_200_OK)


class MarksView(APIView):
    """Return the authenticated student's marks grouped by course."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not hasattr(request.user, 'student'):
            return Response({'message': 'Only students can access this endpoint.'}, status=status.HTTP_403_FORBIDDEN)
        student_courses = StudentCourse.objects.filter(student=request.user.student).select_related('course')
        payload = []
        for student_course in student_courses:
            marks = student_course.marks_set.all()
            payload.append({
                'course_id': student_course.course_id,
                'course_name': student_course.course.name,
                'marks': {mark.name: mark.marks1 for mark in marks},
            })
        return Response({'user_marks': payload}, status=status.HTTP_200_OK)


class TimetableView(APIView):
    """Return the timetable for the student's current cohort session and semester."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not hasattr(request.user, 'student'):
            return Response({'message': 'Only students can access this endpoint.'}, status=status.HTTP_403_FORBIDDEN)
        student = request.user.student
        if not student.current_session_id:
            return Response({'user_timetable': []}, status=status.HTTP_200_OK)
        slots = TimetableSlot.objects.filter(
            course=student.course,
            department=student.dept,
            session=student.current_session,
            semester=student.current_semester,
        ).select_related('subject', 'teacher', 'session').order_by('day', 'start_time')
        serializer = api_ser.TimetableSlotSerializer(slots, many=True, context={'request': request})
        return Response({'user_timetable': serializer.data}, status=status.HTTP_200_OK)


class TeacherAttendanceHistoryView(APIView):
    """Return attendance history for the authenticated teacher."""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        if not hasattr(request.user, 'teacher'):
            return Response({'message': 'Only teachers can access this endpoint.'}, status=status.HTTP_403_FORBIDDEN)
        records = AttendanceRecord.objects.filter(
            timetable_slot__teacher=request.user.teacher,
            date__lte=timezone.localdate(),
        ).select_related(
            'timetable_slot', 'timetable_slot__subject', 'timetable_slot__session',
        ).prefetch_related('student_attendances').order_by('-date', '-id')
        serializer = api_ser.TeacherAttendanceHistorySerializer(records, many=True, context={'request': request})
        return Response({'attendance_history': serializer.data}, status=status.HTTP_200_OK)


class RegistrationMetaView(APIView):
    """Public metadata required for registration forms."""
    permission_classes = [AllowAny, ]

    def get(self, request):
        departments = Dept.objects.all().order_by('name')
        courses = Course.objects.all().order_by('name')
        sessions = Session.objects.all().order_by('-name')

        return Response({
            'departments': api_ser.DeptSerializer(departments, many=True).data,
            'courses': api_ser.CourseSerializer(courses, many=True).data,
            'sessions': api_ser.SessionSerializer(sessions, many=True).data,
            'sex_choices': [x[0] for x in sex_choice],
            'subject_types': [x[0] for x in Subject.SUBJECT_TYPE_CHOICES],
            'semester_choices': [x[0] for x in Subject.SEMESTER_CHOICES],
        }, status=status.HTTP_200_OK)


class StudentRegistrationRequestView(APIView):
    """Create a pending student registration request."""
    permission_classes = [AllowAny, ]

    @staticmethod
    def _generate_sessions_for_admission(admission_year, duration):
        generated = []
        existing_sessions = list(Session.objects.all().only('id', 'name'))
        for offset in range(duration):
            start_year = admission_year + offset
            canonical_name = canonical_academic_session_name(f"{start_year}-{start_year + 1}")
            session = next(
                (item for item in existing_sessions
                 if canonical_academic_session_name(item.name) == canonical_name),
                None,
            )
            if session is None:
                session, _ = Session.objects.get_or_create(name=canonical_name)
                existing_sessions.append(session)
            generated.append(session)
        return generated

    def post(self, request):
        serializer = api_ser.StudentRegistrationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        email = data['email'].strip().lower()

        if data['password'] != data['confirm_password']:
            return Response({'message': 'Passwords do not match.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(data['password']) < 8:
            return Response({'message': 'Password must contain at least 8 characters.'}, status=status.HTTP_400_BAD_REQUEST)
        if (PendingStudent.objects.filter(roll=data['roll']).exists()
                or Student.objects.filter(roll=data['roll']).exists()
                or RejectedStudent.objects.filter(roll=data['roll']).exists()
                or User.objects.filter(username=str(data['roll'])).exists()):
            return Response({'message': 'Roll number already registered or awaiting an admin decision.'}, status=status.HTTP_400_BAD_REQUEST)
        if (PendingStudent.objects.filter(email__iexact=email).exists()
                or Student.objects.filter(email__iexact=email).exists()
                or RejectedStudent.objects.filter(email__iexact=email).exists()
                or User.objects.filter(email__iexact=email).exists()):
            return Response({'message': 'Email already registered or awaiting an admin decision.'}, status=status.HTTP_400_BAD_REQUEST)

        course = get_object_or_404(Course, id=data['course_id'])
        department = get_object_or_404(Dept, id=data['department_id'])
        duration = course.duration_years
        if not duration or not 1 <= int(duration) <= 4:
            return Response({'message': 'The selected course must have a configured duration between one and four years. Contact the college administrator.'}, status=status.HTTP_400_BAD_REQUEST)
        allowed_department_ids = set(course.departments.values_list('id', flat=True))
        if course.dept_id:
            allowed_department_ids.add(course.dept_id)
        if department.pk not in allowed_department_ids:
            return Response({'message': 'The selected department is not available for this course.'}, status=status.HTTP_400_BAD_REQUEST)

        semester = data.get('current_semester', 1)
        max_semesters = min(8, int(duration) * 2)
        if not 1 <= semester <= max_semesters:
            return Response({'message': 'The selected semester is outside the course duration.'}, status=status.HTTP_400_BAD_REQUEST)
        sessions = self._generate_sessions_for_admission(data['admission_year'], int(duration))
        current_session = sessions[(semester - 1) // 2]

        with transaction.atomic():
            pending = PendingStudent.objects.create(
                roll=data['roll'],
                name=data['name'].strip(),
                email=email,
                sex=data['sex'],
                department=department,
                phone=data['phone'],
                dob=data['dob'],
                admission_year=data['admission_year'],
                password=make_password(data['password']),
                course=course,
                current_semester=semester,
                current_session=current_session,
            )
            pending.sessions.set(sessions)

        return Response({'message': 'Registration request submitted.', 'pending_student_id': pending.id}, status=status.HTTP_201_CREATED)


class TeacherRegistrationRequestView(APIView):
    """Create a pending teacher registration request."""
    permission_classes = [AllowAny, ]

    def post(self, request):
        serializer = api_ser.TeacherRegistrationRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        email = data['email'].strip().lower()

        if data['password'] != data['confirm_password']:
            return Response({'message': 'Passwords do not match.'}, status=status.HTTP_400_BAD_REQUEST)
        if len(data['password']) < 8:
            return Response({'message': 'Password must contain at least 8 characters.'}, status=status.HTTP_400_BAD_REQUEST)
        if (PendingTeacher.objects.filter(email__iexact=email).exists()
                or Teacher.objects.filter(email__iexact=email).exists()
                or RejectedTeacher.objects.filter(email__iexact=email).exists()
                or User.objects.filter(email__iexact=email).exists()):
            return Response({'message': 'Email already registered or awaiting an admin decision.'}, status=status.HTTP_400_BAD_REQUEST)

        course = get_object_or_404(Course, id=data['course_id'])
        department = get_object_or_404(Dept, id=data['department_id'])
        allowed_department_ids = set(course.departments.values_list('id', flat=True))
        if course.dept_id:
            allowed_department_ids.add(course.dept_id)
        if department.pk not in allowed_department_ids:
            return Response({'message': 'The selected department is not available for this course.'}, status=status.HTTP_400_BAD_REQUEST)

        local_part = (email.split('@', 1)[0] or 'teacher')[:70]
        suffix = hashlib.sha256(email.encode('utf-8')).hexdigest()[:10]
        username = f'{local_part}_{suffix}'
        if PendingTeacher.objects.filter(username__iexact=username).exists() or User.objects.filter(username__iexact=username).exists():
            return Response({'message': 'An account with this username already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        pending = PendingTeacher.objects.create(
            username=username,
            dept=department,
            password=make_password(data['password']),
            phone=data['phone'],
            name=data['name'].strip(),
            email=email,
            dob=data['dob'],
            course=course,
            sex=data['sex'],
        )
        return Response({'message': 'Registration request submitted.', 'pending_teacher_id': pending.id}, status=status.HTTP_201_CREATED)


class TeacherSubjectsManageView(APIView):
    """List and create subjects for authenticated teacher's dept/course."""
    permission_classes = [IsAuthenticated, ]

    def get(self, request):
        if not hasattr(request.user, 'teacher'):
            return Response({'message': 'Only teachers can access this endpoint.'}, status=status.HTTP_403_FORBIDDEN)

        teacher = request.user.teacher
        subjects = Subject.objects.filter(
            department=teacher.dept,
            course=teacher.course,
        ).select_related('session').prefetch_related('teachers').order_by('-id')

        return Response({'subjects': api_ser.SubjectManageSerializer(subjects, many=True).data}, status=status.HTTP_200_OK)

    def post(self, request):
        if not hasattr(request.user, 'teacher'):
            return Response({'message': 'Only teachers can access this endpoint.'}, status=status.HTTP_403_FORBIDDEN)

        serializer = api_ser.SubjectCreateRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data

        teacher = request.user.teacher
        if Subject.objects.filter(code=data['code']).exists():
            return Response({'message': 'Subject code already exists.'}, status=status.HTTP_400_BAD_REQUEST)

        subject = Subject.objects.create(
            name=data['name'],
            code=data['code'],
            type=data['type'],
            session_id=data['session_id'],
            semester=data['semester'],
            department=teacher.dept,
            course=teacher.course,
        )

        teacher_ids = data.get('teacher_ids') or [teacher.id]
        valid_teacher_ids = list(Teacher.objects.filter(id__in=teacher_ids, dept=teacher.dept).values_list('id', flat=True))
        if valid_teacher_ids:
            subject.teachers.set(valid_teacher_ids)

        students = Student.objects.filter(
            current_semester=subject.semester,
            dept=teacher.dept,
            course=teacher.course,
            current_session=subject.session,
        )
        for stud in students:
            stud.subjects.add(subject)

        return Response({'message': 'Subject created successfully.', 'subject': api_ser.SubjectManageSerializer(subject).data}, status=status.HTTP_201_CREATED)


class TeacherSubjectDeleteView(APIView):
    """Delete subject for authenticated teacher scope."""
    permission_classes = [IsAuthenticated, ]

    def delete(self, request, subject_id):
        if not hasattr(request.user, 'teacher'):
            return Response({'message': 'Only teachers can access this endpoint.'}, status=status.HTTP_403_FORBIDDEN)

        teacher = request.user.teacher
        subject = get_object_or_404(
            Subject,
            id=subject_id,
            department=teacher.dept,
            course=teacher.course,
        )
        subject.teachers.clear()
        subject.delete()
        return Response({'message': 'Subject deleted successfully.'}, status=status.HTTP_200_OK)
