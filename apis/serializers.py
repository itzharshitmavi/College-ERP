from rest_framework import serializers
from django.urls import reverse

from info.models import *


class DetailSerializer(serializers.ModelSerializer):
    class Meta:
        model = Student
        fields = '__all__'


class AttendanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendanceSummary
        fields = '__all__'


class StudentAttendanceSummarySerializer(serializers.Serializer):
    subject_id = serializers.IntegerField()
    subject_name = serializers.CharField()
    subject_code = serializers.CharField(allow_blank=True)
    total_classes = serializers.IntegerField()
    classes_attended = serializers.IntegerField()
    classes_absent = serializers.IntegerField()
    attendance_percentage = serializers.FloatField()
    classes_to_attend = serializers.IntegerField()


class AttendanceRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = AttendanceRecord
        fields = '__all__'


class StudentAttendanceSerializer(serializers.ModelSerializer):
    class Meta:
        model = StudentAttendance
        fields = '__all__'


class MarksSerializer(serializers.ModelSerializer):
    class Meta:
        model = Marks
        fields = '__all__'


class TimeTableSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssignTime
        fields = '__all__'


class TimetableSlotSerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source='subject.name', read_only=True)
    teacher_name = serializers.CharField(source='teacher.name', read_only=True)
    session_name = serializers.CharField(source='session.name', read_only=True)

    class Meta:
        model = TimetableSlot
        fields = [
            'id',
            'day',
            'start_time',
            'end_time',
            'semester',
            'session',
            'session_name',
            'subject',
            'subject_name',
            'teacher',
            'teacher_name',
            'course',
            'department',
        ]


class TeacherAttendanceHistorySerializer(serializers.ModelSerializer):
    subject_name = serializers.CharField(source='timetable_slot.subject.name', read_only=True)
    session_name = serializers.CharField(source='timetable_slot.session.name', read_only=True)
    semester = serializers.IntegerField(source='timetable_slot.semester', read_only=True)
    day = serializers.CharField(source='timetable_slot.day', read_only=True)
    start_time = serializers.TimeField(source='timetable_slot.start_time', read_only=True)
    end_time = serializers.TimeField(source='timetable_slot.end_time', read_only=True)
    status = serializers.SerializerMethodField()
    has_pdf = serializers.SerializerMethodField()
    pdf_url = serializers.SerializerMethodField()

    class Meta:
        model = AttendanceRecord
        fields = [
            'id',
            'date',
            'subject_name',
            'session_name',
            'semester',
            'day',
            'start_time',
            'end_time',
            'status',
            'has_pdf',
            'pdf_url',
        ]

    def get_status(self, obj):
        if not obj.session_held:
            return 'cancelled'
        if obj.student_attendances.exists():
            return 'marked'
        return 'not_marked'

    def get_has_pdf(self, obj):
        return obj.session_held and obj.student_attendances.exists()

    def get_pdf_url(self, obj):
        if not self.get_has_pdf(obj):
            return None
        request = self.context.get('request')
        url = reverse('download_attendance_pdf', args=[obj.id])
        return request.build_absolute_uri(url) if request else url


class DeptSerializer(serializers.ModelSerializer):
    class Meta:
        model = Dept
        fields = ['id', 'name']


class CourseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Course
        fields = ['id', 'name', 'shortname', 'duration_years', 'dept']


class SessionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Session
        fields = ['id', 'name', 'is_current']


class SubjectManageSerializer(serializers.ModelSerializer):
    session_name = serializers.CharField(source='session.name', read_only=True)
    teacher_ids = serializers.PrimaryKeyRelatedField(source='teachers', many=True, read_only=True)

    class Meta:
        model = Subject
        fields = [
            'id',
            'name',
            'code',
            'type',
            'semester',
            'course',
            'department',
            'session',
            'session_name',
            'teacher_ids',
        ]


class StudentRegistrationRequestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.RegexField(regex=r'^\d{10}$')
    dob = serializers.DateField()
    course_id = serializers.IntegerField(min_value=1)
    department_id = serializers.CharField(max_length=100)
    roll = serializers.IntegerField(min_value=1, max_value=9223372036854775807)
    sex = serializers.ChoiceField(choices=sex_choice)
    password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)
    admission_year = serializers.IntegerField(min_value=2000, max_value=2100)
    current_semester = serializers.IntegerField(required=False, default=1, min_value=1, max_value=8)


class TeacherRegistrationRequestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    email = serializers.EmailField()
    phone = serializers.RegexField(regex=r'^\d{10}$')
    course_id = serializers.IntegerField(min_value=1)
    department_id = serializers.CharField(max_length=100)
    sex = serializers.ChoiceField(choices=sex_choice)
    dob = serializers.DateField()
    password = serializers.CharField(write_only=True)
    confirm_password = serializers.CharField(write_only=True)


class SubjectCreateRequestSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100)
    code = serializers.CharField(max_length=20)
    type = serializers.ChoiceField(choices=Subject.SUBJECT_TYPE_CHOICES)
    session_id = serializers.IntegerField()
    semester = serializers.IntegerField(min_value=1, max_value=8)
    teacher_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, allow_empty=True
    )
