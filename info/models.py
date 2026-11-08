from django.db import models, transaction
from django.conf import settings
from django.db.models import Sum
from decimal import Decimal
import math
from django.core.validators import MinValueValidator, MaxValueValidator
from django.contrib.auth.models import AbstractUser
from django.db.models.signals import post_save, post_delete
from datetime import timedelta
from django.utils import timezone
# Create your models here.
sex_choice = (
    ('Male', 'Male'),
    ('Female', 'Female'),
    ('Other', 'Other'),
)

STATUS_CHOICES = (
    ('Pending', 'Pending'),
    ('Approved', 'Approved'),
    ('Rejected', 'Rejected'),
)

time_slots = (
    ('7:30 - 8:30', '7:30 - 8:30'),
    ('8:30 - 9:30', '8:30 - 9:30'),
    ('9:30 - 10:30', '9:30 - 10:30'),
    ('11:00 - 11:50', '11:00 - 11:50'),
    ('11:50 - 12:40', '11:50 - 12:40'),
    ('12:40 - 1:30', '12:40 - 1:30'),
    ('2:30 - 3:30', '2:30 - 3:30'),
    ('3:30 - 4:30', '3:30 - 4:30'),
    ('4:30 - 5:30', '4:30 - 5:30'),
)

DAYS_OF_WEEK = (
    ('Monday', 'Monday'),
    ('Tuesday', 'Tuesday'),
    ('Wednesday', 'Wednesday'),
    ('Thursday', 'Thursday'),
    ('Friday', 'Friday'),
    ('Saturday', 'Saturday'),
)

def canonical_academic_session_name(raw_name):
    if raw_name is None:
        return ''
    parts = [part.strip() for part in str(raw_name).strip().replace('/', '-').split('-') if part.strip()]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return str(raw_name).strip()
    start = int(parts[0])
    end = int(parts[1])
    if len(parts[1]) == 2 and len(parts[0]) == 4:
        end += (start // 100) * 100
        if end < start:
            end += 100
    return f'{start}-{end}'


test_name = (
    ('Internal test 1', 'Internal test 1'),
    ('Internal test 2', 'Internal test 2'),
    ('Internal test 3', 'Internal test 3'),
    ('Event 1', 'Event 1'),
    ('Event 2', 'Event 2'),
    ('Semester End Exam', 'Semester End Exam'),
)



class User(AbstractUser):
    @property
    def is_student(self):
        if hasattr(self, 'student'):
            return True
        return False

    @property
    def is_teacher(self):
        if hasattr(self, 'teacher'):
            return True
        return False


class Dept(models.Model):
    id = models.CharField(primary_key='True', max_length=100)
    name = models.CharField(max_length=200)

    def __str__(self):
        return self.name
    


class Course(models.Model):
    dept = models.ForeignKey(Dept,related_name='primary_courses' ,on_delete=models.CASCADE, null=True, blank=True)
    id = models.AutoField(primary_key=True) 
    name = models.CharField(max_length=50)
    shortname = models.CharField(max_length=50, default='X')
    duration_years = models.PositiveSmallIntegerField(null=True)
    departments = models.ManyToManyField(Dept,related_name='courses' ,blank=True) 
    def __str__(self):
        return self.name



class PendingTeacher(models.Model):
    username = models.CharField(max_length=100, unique=True, null=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    dept = models.ForeignKey(Dept, on_delete=models.CASCADE, null=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True)
    phone = models.CharField(max_length=15, null=True)
    password = models.CharField(max_length=128, null=True)
    dob = models.DateField(null=True, blank=True)
    sex = models.CharField(max_length=10, choices=sex_choice, null=True)
    photo = models.ImageField(upload_to='teacher_photos/', blank=True, null=True)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, null=True, related_name='pending_teacher')
    def __str__(self):
        return self.name


class RejectedTeacher(models.Model):
    username = models.CharField(max_length=100, unique=True, null=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    dept = models.ForeignKey(Dept, on_delete=models.CASCADE, null=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, null=True)
    phone = models.CharField(max_length=15, null=True)
    password = models.CharField(max_length=128, null=True)
    dob = models.DateField(null=True, blank=True)
    sex = models.CharField(max_length=10, choices=sex_choice, null=True)
    photo = models.ImageField(upload_to='teacher_photos/', blank=True, null=True)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, null=True, related_name='rejected_teacher')

    def __str__(self):
        return self.name


class Class(models.Model):
    # courses = models.ManyToManyField(Course, default=1)
    id = models.CharField(primary_key='True', max_length=100)
    dept = models.ForeignKey(Dept, on_delete=models.CASCADE)
    section = models.CharField(max_length=100)

    class Meta:
        verbose_name_plural = 'classes'

    def __str__(self):
        return f'{self.dept.name} : {self.id} / {self.section}'


class Session(models.Model):
    name = models.CharField(
        max_length=20,
        unique=True,
        help_text="Academic session, e.g. '2024-2025'",
        null=True
    )
    is_current = models.BooleanField(
        default=False, 
        help_text="Designates the current active session",
        null=True
    )

    def save(self, *args, **kwargs):
        if self.name:
            self.name = canonical_academic_session_name(self.name)
        if self.is_current:
            Session.objects.filter(is_current=True).exclude(pk=self.pk).update(is_current=False)
        super().save(*args, **kwargs)


    class Meta:
        ordering = ['-name']  # show latest first

    def __str__(self):
        return self.name


class Subject(models.Model):
    THEORY = 'Theory'
    PRACTICAL = 'Practical'
    SUBJECT_TYPE_CHOICES = [
        (THEORY, 'Theory'),
        (PRACTICAL, 'Practical'),
    ]
    SEMESTER_CHOICES = [
        (1, 'Semester 1'),
        (2, 'Semester 2'),
        (3, 'Semester 3'),
        (4, 'Semester 4'),
        (5, 'Semester 5'),
        (6, 'Semester 6'),
        (7, 'Semester 7'),
        (8, 'Semester 8'),
    ]

    name = models.CharField(max_length=100)
    code = models.CharField(max_length=20, unique=True)
    type = models.CharField(max_length=10, choices=SUBJECT_TYPE_CHOICES, default=THEORY)
    semester = models.PositiveSmallIntegerField(
        choices=SEMESTER_CHOICES,
        default=1,
        help_text='Semester number (continuous across years)'
    )

    department = models.ForeignKey(Dept, on_delete=models.CASCADE, related_name='subjects')
    session = models.ForeignKey('Session', on_delete=models.CASCADE, related_name='subjects')
    teachers = models.ManyToManyField('Teacher', blank=True, related_name='assigned_subjects')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='subjects')


    def __str__(self):
        return f"{self.name} ({self.code}) - {self.department.name} - Sem {self.semester} [{self.type}]"
    
class Student(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True)
    name = models.CharField(max_length=200)
    roll = models.PositiveBigIntegerField(null=True,blank=True)
    phone = models.CharField(max_length=10, null=True,blank=True)
    sex = models.CharField(max_length=10, choices=sex_choice, null=True)
    dob = models.DateField(null=True, blank=True)
    dept = models.ForeignKey(Dept, on_delete=models.CASCADE, null=True)
    sessions = models.ManyToManyField(Session, related_name='students', blank=True) 
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Pending')
    photo = models.ImageField(upload_to='student_photos/', blank=True, null=True)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, null=True, related_name='students')
    admission_year = models.PositiveIntegerField(null=True, blank=True)
    email = models.EmailField(unique=True, null=True)
    current_semester = models.PositiveSmallIntegerField(default=1)
    current_session = models.ForeignKey(Session, on_delete=models.SET_NULL, null=True, blank=True)
    subjects = models.ManyToManyField(Subject, blank=True)
    father_name = models.CharField(max_length=200, null=True, blank=True)
    mother_name = models.CharField(max_length=200, null=True, blank=True)
    guardian_name = models.CharField(max_length=200, null=True, blank=True)
    guardian_relation = models.CharField(max_length=100, null=True, blank=True)
    guardian_phone = models.CharField(max_length=15, null=True, blank=True)
    family_address = models.TextField(null=True, blank=True)
    family_city = models.CharField(max_length=100, null=True, blank=True)
    family_state = models.CharField(max_length=100, null=True, blank=True)
    family_pincode = models.CharField(max_length=10, null=True, blank=True)
    family_details_completed = models.BooleanField(default=False)
    ACADEMIC_STATUS_ENROLLED = 'Enrolled'
    ACADEMIC_STATUS_GRADUATED = 'Graduated'
    ACADEMIC_STATUS_DISCONTINUED = 'Discontinued'
    ACADEMIC_STATUS_CHOICES = [
        (ACADEMIC_STATUS_ENROLLED, 'Enrolled'),
        (ACADEMIC_STATUS_GRADUATED, 'Graduated'),
        (ACADEMIC_STATUS_DISCONTINUED, 'Discontinued'),
    ]
    academic_status = models.CharField(max_length=20, choices=ACADEMIC_STATUS_CHOICES, default=ACADEMIC_STATUS_ENROLLED)
    graduated_at = models.DateField(null=True, blank=True)

    def max_semesters(self):
        """Return the maximum semester supported by this student's configured course."""
        if not self.course_id or not self.course.duration_years:
            return 0
        return min(8, int(self.course.duration_years) * 2)

    def session_for_semester(self, semester):
        """Resolve the cohort-specific academic session for a semester.

        Semesters 1-2 belong to the admission-year session, semesters 3-4
        to the following session, and so on. This deliberately does not use
        Session.is_current: that flag is the college calendar, not a student's
        batch/session assignment.
        """
        if not self.admission_year or int(semester) < 1:
            return None
        year_offset = (int(semester) - 1) // 2
        start_year = int(self.admission_year) + year_offset
        session_name = f"{start_year}-{start_year + 1}"
        for existing in Session.objects.all().only('id', 'name'):
            if canonical_academic_session_name(existing.name) == session_name:
                return existing
        session, _ = Session.objects.get_or_create(name=session_name)
        return session

    def assign_current_subjects(self):
        """Synchronise the student's subject list with their current position."""
        if not self.course_id or not self.dept_id or not self.current_session_id:
            return 0

        matching_subjects = Subject.objects.filter(
            course_id=self.course_id,
            department_id=self.dept_id,
            semester=self.current_semester,
            session_id=self.current_session_id,
        )
        self.subjects.set(matching_subjects)
        return matching_subjects.count()

    def set_academic_position(self, semester, actor=None, allow_same=True):
        """Move the student to a valid semester and its matching cohort session.

        Progression is transactional and logged. The unique student/from/to
        transition prevents a repeated submit from promoting the same student
        twice for the same semester transition.
        """
        semester = int(semester)
        if semester < 1 or semester > self.max_semesters():
            return False
        if semester < self.current_semester:
            return False
        if semester == self.current_semester and not allow_same:
            return False

        with transaction.atomic():
            locked = Student.objects.select_for_update().get(pk=self.pk)
            old_semester = locked.current_semester
            old_session = locked.current_session
            target_session = locked.session_for_semester(semester)
            if not target_session:
                return False

            if semester > old_semester:
                _, created = AcademicProgressionLog.objects.get_or_create(
                    student=locked,
                    from_semester=old_semester,
                    to_semester=semester,
                    defaults={
                        'from_session': old_session,
                        'to_session': target_session,
                        'performed_by': actor if getattr(actor, 'is_authenticated', False) else None,
                    },
                )
                if not created:
                    return False

            locked.current_semester = semester
            locked.current_session = target_session
            locked.save(update_fields=['current_semester', 'current_session'])
            locked.sessions.add(target_session)
            locked.assign_current_subjects()

        self.refresh_from_db(fields=['current_semester', 'current_session'])
        return True

    def promote_semester(self, actor=None):
        """Advance one semester only when its cohort session is the college's active session.

        This makes the operation repeat-safe within an academic year: after Sem 1
        advances to Sem 2, a second request cannot advance to Sem 3 until the
        college activates the next academic session.
        """
        if self.academic_status != self.ACADEMIC_STATUS_ENROLLED:
            return False
        next_semester = self.current_semester + 1
        if next_semester > self.max_semesters():
            return False
        target_session = self.session_for_semester(next_semester)
        active_session = Session.objects.filter(is_current=True).first()
        if not target_session or not active_session or target_session.pk != active_session.pk:
            return False
        return self.set_academic_position(next_semester, actor=actor, allow_same=False)

    def promote_session(self):
        """Repair cohort session/subject links without changing the semester."""
        if not self.admission_year or not self.course_id or not self.course.duration_years:
            return False
        target_session = self.session_for_semester(self.current_semester)
        if not target_session:
            return False
        cohort_sessions = [
            self.session_for_semester(semester)
            for semester in range(1, self.max_semesters() + 1, 2)
        ]
        self.current_session = target_session
        self.save(update_fields=['current_session'])
        self.sessions.set([session for session in cohort_sessions if session])
        self.assign_current_subjects()
        return True

    def get_current_subjects(self):
        if not self.course_id or not self.dept_id or not self.current_session_id:
            return Subject.objects.none()
        return Subject.objects.filter(
            course_id=self.course_id,
            department_id=self.dept_id,
            session_id=self.current_session_id,
            semester=self.current_semester,
        )

    def __str__(self):
        return self.name

class PendingStudent(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    department = models.ForeignKey(Dept, on_delete=models.CASCADE, null=True)
    roll = models.PositiveBigIntegerField(null=True,blank=True)
    phone = models.CharField(max_length=10, null=True,blank=True)
    sex = models.CharField(max_length=10, choices=sex_choice, null=True)
    password = models.CharField(max_length=128, null=True)
    dob = models.DateField(null=True, blank=True)
    sessions = models.ManyToManyField(Session, related_name='pending_students', blank=True) 
    photo = models.ImageField(upload_to='student_photos/', blank=True, null=True)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, null=True, related_name='pending_students')
    admission_year = models.PositiveIntegerField(null=True, blank=True)
    current_semester = models.PositiveSmallIntegerField(default=1)
    current_session = models.ForeignKey(Session, on_delete=models.SET_NULL, null=True, blank=True)


    def __str__(self):
        return self.name
    
class RejectedStudent(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True)
    department = models.ForeignKey(Dept, on_delete=models.CASCADE, null=True)
    roll = models.PositiveBigIntegerField(null=True,blank=True)
    phone = models.CharField(max_length=10, null=True,blank=True)
    sex = models.CharField(max_length=10, choices=sex_choice, null=True)
    password = models.CharField(max_length=128, null=True)
    dob = models.DateField(null=True, blank=True)
    sessions = models.ManyToManyField(Session, related_name='rejected_students', blank=True) 
    photo = models.ImageField(upload_to='student_photos/', blank=True, null=True)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, null=True, related_name='rejected_students')
    admission_year = models.PositiveIntegerField(null=True, blank=True)
    current_semester = models.PositiveSmallIntegerField(default=1)
    current_session = models.ForeignKey(Session, on_delete=models.SET_NULL, null=True, blank=True)


    def __str__(self):
        return self.name

class Teacher(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, null=True)
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True,null=True)
    phone = models.CharField(max_length=10, null=True,blank=True)
    sex = models.CharField(max_length=10, choices=sex_choice, null=True)
    dob = models.DateField(null=True, blank=True)
    dept = models.ForeignKey(Dept, on_delete=models.CASCADE, null=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='Pending')
    photo = models.ImageField(upload_to='teacher_photos/', blank=True, null=True)
    course = models.ForeignKey(Course, on_delete=models.CASCADE, null=True, related_name='teacher')

    def __str__(self):
        return self.name


class HODAssignment(models.Model):
    YEAR_CHOICES = [
        (1, 'Year 1'),
        (2, 'Year 2'),
        (3, 'Year 3'),
        (4, 'Year 4'),
    ]

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='hod_assignments')
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='hod_assignments')
    department = models.ForeignKey(Dept, on_delete=models.CASCADE, related_name='hod_assignments')
    year = models.PositiveSmallIntegerField(choices=YEAR_CHOICES)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = (('course', 'department', 'year'),)
        ordering = ['course__name', 'department__name', 'year']

    def clean(self):
        if self.teacher_id:
            if self.teacher.course_id and self.course_id and self.teacher.course_id != self.course_id:
                from django.core.exceptions import ValidationError
                raise ValidationError({'teacher': 'Teacher course must match the assigned course.'})
            if self.teacher.dept_id and self.department_id and self.teacher.dept_id != self.department_id:
                from django.core.exceptions import ValidationError
                raise ValidationError({'teacher': 'Teacher department must match the assigned department.'})

    def __str__(self):
        status = 'Active' if self.is_active else 'Inactive'
        return f"{self.teacher.name} - {self.course.shortname} / {self.department.name} / Year {self.year} ({status})"


class Assign(models.Model):
    session = models.ForeignKey(Session, on_delete=models.CASCADE, null=True)
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)

    class Meta:
        unique_together = (('course', 'session', 'teacher'),)

    def __str__(self):
        cl = Session.objects.get(id=self.session_id)
        cr = Course.objects.get(id=self.course_id)
        te = Teacher.objects.get(id=self.teacher_id)
        return '%s : %s : %s' % (te.name, cr.shortname, cl)


class AssignTime(models.Model):
    assign = models.ForeignKey(Assign, on_delete=models.CASCADE)
    period = models.CharField(max_length=50, choices=time_slots, default='11:00 - 11:50')
    day = models.CharField(max_length=15, choices=DAYS_OF_WEEK)


class StudentCourse(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE)
    course = models.ForeignKey(Course, on_delete=models.CASCADE)

    class Meta:
        unique_together = (('student', 'course'),)
        verbose_name_plural = 'Marks'

    def __str__(self):
        return f'{self.student.name} : {self.course.shortname}'

    def get_cie(self):
        marks_list = self.marks_set.all()
        m = []
        for mk in marks_list:
            m.append(mk.marks1)
        cie = math.ceil(sum(m[:5]) / 2)
        return cie


class Marks(models.Model):
    studentcourse = models.ForeignKey(StudentCourse, on_delete=models.CASCADE)
    name = models.CharField(max_length=50, choices=test_name, default='Internal test 1')
    marks1 = models.IntegerField(default=0, validators=[MinValueValidator(0), MaxValueValidator(100)])

    class Meta:
        unique_together = (('studentcourse', 'name'),)

    @property
    def total_marks(self):
        if self.name == 'Semester End Exam':
            return 100
        return 20


class MarksClass(models.Model):
    assign = models.ForeignKey(Assign, on_delete=models.CASCADE)
    name = models.CharField(max_length=50, choices=test_name, default='Internal test 1')
    status = models.BooleanField(default='False')

    class Meta:
        unique_together = (('assign', 'name'),)

    @property
    def total_marks(self):
        if self.name == 'Semester End Exam':
            return 100
        return 20


class AttendanceRange(models.Model):
    start_date = models.DateField()
    end_date = models.DateField()


# Triggers


def daterange(start_date, end_date):
    for n in range(int((end_date - start_date).days)):
        yield start_date + timedelta(n)


days = {
    'Monday': 1,
    'Tuesday': 2,
    'Wednesday': 3,
    'Thursday': 4,
    'Friday': 5,
    'Saturday': 6,
}


def create_marks(sender, instance, **kwargs):
    if kwargs['created']:
        if hasattr(instance, 'name'):
            for session in instance.sessions.all():
                ass_list = Assign.objects.filter(session=session)
                for ass in ass_list:
                    try:
                        StudentCourse.objects.get(student=instance, course=ass.course)
                    except StudentCourse.DoesNotExist:
                        sc = StudentCourse(student=instance, course=ass.course)
                        sc.save()
                        sc.marks_set.create(name='Internal test 1')
                        sc.marks_set.create(name='Internal test 2')
                        sc.marks_set.create(name='Internal test 3')
                        sc.marks_set.create(name='Event 1')
                        sc.marks_set.create(name='Event 2')
                        sc.marks_set.create(name='Semester End Exam')
        elif hasattr(instance, 'course'):
            stud_list = Student.objects.filter(sessions=instance.session)
            cr = instance.course
            for s in stud_list:
                try:
                    StudentCourse.objects.get(student=s, course=cr)
                except StudentCourse.DoesNotExist:
                    sc = StudentCourse(student=s, course=cr)
                    sc.save()
                    sc.marks_set.create(name='Internal test 1')
                    sc.marks_set.create(name='Internal test 2')
                    sc.marks_set.create(name='Internal test 3')
                    sc.marks_set.create(name='Event 1')
                    sc.marks_set.create(name='Event 2')
                    sc.marks_set.create(name='Semester End Exam')


def create_marks_class(sender, instance, **kwargs):
    if kwargs['created']:
        for name in test_name:
            try:
                MarksClass.objects.get(assign=instance, name=name[0])
            except MarksClass.DoesNotExist:
                m = MarksClass(assign=instance, name=name[0])
                m.save()


class SpecialUpdateNote(models.Model):
    title = models.CharField(max_length=200)
    pdf_file = models.FileField(upload_to='special_updates/')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    uploader = models.ForeignKey(User, on_delete=models.CASCADE,null=True)

    def __str__(self):
        return self.title


class NoticeReadState(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='notice_read_state')
    last_seen_notice_id = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - seen notice id {self.last_seen_notice_id}"


class ChatReadState(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='chat_read_state')
    last_seen_message_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.user.username} - last seen chat at {self.last_seen_message_at}"
    
class TimetableSlot(models.Model):
    SEMESTER_CHOICES = [
        (1, 'Semester 1'),
        (2, 'Semester 2'),
        (3, 'Semester 3'),
        (4, 'Semester 4'),
        (5, 'Semester 5'),
        (6, 'Semester 6'),
        (7, 'Semester 7'),
        (8, 'Semester 8'),
    ]
    
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE)
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    department = models.ForeignKey(Dept, on_delete=models.CASCADE, null=True)
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    session = models.ForeignKey(Session, on_delete=models.CASCADE)
    day = models.CharField(max_length=10, choices=DAYS_OF_WEEK)
    semester = models.PositiveSmallIntegerField(choices=SEMESTER_CHOICES, default=1, help_text='Semester for this class')
    start_time = models.TimeField()
    end_time = models.TimeField()
    
    class Meta:
        unique_together = ('teacher', 'day', 'start_time', 'session')  # to avoid slot overlaps

    def __str__(self):
        return f"{self.teacher} - {self.subject} on {self.day} {self.start_time}-{self.end_time}"
    

# Attendance System Models
class AttendanceRecord(models.Model):
    """
    Represents a single class session where attendance was marked.
    Links to TimetableSlot (teacher's schedule) and tracks when/who marked it.
    """
    timetable_slot = models.ForeignKey(TimetableSlot, on_delete=models.CASCADE, related_name='attendance_records')
    date = models.DateField()
    
    # Who marked attendance and when
    marked_by = models.ForeignKey(Teacher, on_delete=models.SET_NULL, null=True, blank=True)
    marked_at = models.DateTimeField(auto_now_add=True)
    
    # Was the class held?
    session_held = models.BooleanField(default=True)
    
    # Optional notes
    remarks = models.TextField(blank=True, null=True)
    
    class Meta:
        unique_together = ('timetable_slot', 'date')
        verbose_name = 'Attendance Record'
        verbose_name_plural = 'Attendance Records'
    
    def __str__(self):
        return f"{self.timetable_slot} - {self.date}"


class StudentAttendance(models.Model):
    """
    Individual student's attendance for a specific class session.
    """
    ATTENDANCE_CHOICES = (
        (True, 'Present'),
        (False, 'Absent'),
    )
    
    attendance_record = models.ForeignKey(AttendanceRecord, on_delete=models.CASCADE, related_name='student_attendances')
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='attendances')
    
    # Attendance status
    status = models.BooleanField(choices=ATTENDANCE_CHOICES)
    
    class Meta:
        unique_together = ('attendance_record', 'student')
        verbose_name = 'Student Attendance'
        verbose_name_plural = 'Student Attendances'
    
    def __str__(self):
        status_text = "Present" if self.status else "Absent"
        return f"{self.student.name} - {self.attendance_record.date} [{status_text}]"


class AttendanceSummary(models.Model):
    """
    Summary of student's attendance for a subject.
    Useful for quick queries and reports.
    """
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='attendance_summaries')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE)
    
    total_classes = models.PositiveIntegerField(default=0)
    classes_attended = models.PositiveIntegerField(default=0)
    
    class Meta:
        unique_together = ('student', 'subject')
        verbose_name = 'Attendance Summary'
        verbose_name_plural = 'Attendance Summaries'
    
    @property
    def attendance_percentage(self):
        if self.total_classes == 0:
            return 0
        return round((self.classes_attended / self.total_classes) * 100, 2)
    
    @property
    def classes_to_attend(self):
        """Calculate how many more classes needed to maintain 75% attendance"""
        if self.total_classes == 0:
            return 0
        cta = math.ceil((0.75 * self.total_classes - self.classes_attended) / 0.25)
        return max(0, cta)
    
    def __str__(self):
        return f"{self.student.name} - {self.subject.name} ({self.attendance_percentage}%)"


class ChatMessage(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='chat_messages')
    message = models.TextField(blank=True)
    photo = models.ImageField(upload_to='chat_photos/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(blank=True, null=True)

    class Meta:
        ordering = ['created_at']

    def __str__(self):
        return f"{self.user.username}: {self.message[:30]}"


class TeacherNote(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='notes')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='teacher_notes')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    file = models.FileField(upload_to='teacher_notes/')
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    department = models.ForeignKey(Dept, on_delete=models.CASCADE)
    session = models.ForeignKey(Session, on_delete=models.CASCADE)
    semester = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.subject.code})"


class Exam(models.Model):
    EXAM_TYPE_CHOICES = [
        ('Sessional 1', 'Sessional 1'),
        ('Sessional 2', 'Sessional 2'),
        ('Mid Term', 'Mid Term'),
        ('Internal Practical', 'Internal Practical'),
    ]
    
    STATUS_CHOICES = [
        ('Pending', 'Pending'),
        ('Conducted', 'Conducted'),
    ]
    
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='exams')
    exam_type = models.CharField(max_length=20, choices=EXAM_TYPE_CHOICES)
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='exams')
    semester = models.PositiveSmallIntegerField(
        choices=Subject.SEMESTER_CHOICES,
        default=1,
    )
    date_conducted = models.DateField(null=True, blank=True)
    exam_time = models.TimeField(null=True, blank=True)
    total_marks = models.PositiveSmallIntegerField(default=25)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default='Pending')
    created_by = models.ForeignKey(Teacher, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_exams')
    scheduled_students = models.ManyToManyField('Student', blank=True, related_name='scheduled_exams')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        unique_together = ('subject', 'exam_type', 'session', 'semester')
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.subject.name} - {self.exam_type} ({self.session.name}) - {self.status}"


class ExamMark(models.Model):
    exam = models.ForeignKey(Exam, on_delete=models.CASCADE, related_name='marks_entries')
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='exam_marks')
    marks_obtained = models.PositiveSmallIntegerField(null=True, blank=True)
    is_absent = models.BooleanField(default=False)
    entered_by = models.ForeignKey(Teacher, on_delete=models.SET_NULL, null=True, blank=True, related_name='entered_exam_marks')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('exam', 'student')
        ordering = ['student__roll', 'student__name']

    def __str__(self):
        return f"{self.exam} - {self.student.name}"


class TeacherLeave(models.Model):
    STATUS_PENDING = 'Pending'
    STATUS_APPROVED = 'Approved'
    STATUS_REJECTED = 'Rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='leave_requests')
    from_date = models.DateField()
    to_date = models.DateField()
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    admin_remark = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_teacher_leaves')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.teacher.name}: {self.from_date} to {self.to_date} ({self.status})"


class StudentLeave(models.Model):
    STATUS_PENDING = 'Pending'
    STATUS_APPROVED = 'Approved'
    STATUS_REJECTED = 'Rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_APPROVED, 'Approved'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='leave_requests')
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='student_leave_requests')
    from_date = models.DateField()
    to_date = models.DateField()
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    teacher_remark = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_student_leaves')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.student.name} -> {self.teacher.name}: {self.from_date} to {self.to_date} ({self.status})"


class StudentCounselingRequest(models.Model):
    STATUS_PENDING = 'Pending'
    STATUS_IN_PROGRESS = 'In Progress'
    STATUS_RESOLVED = 'Resolved'
    STATUS_REJECTED = 'Rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_IN_PROGRESS, 'In Progress'),
        (STATUS_RESOLVED, 'Resolved'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='counseling_requests')
    hod_teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='assigned_counseling_requests')
    subject = models.CharField(max_length=150)
    concern = models.TextField()
    preferred_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    hod_remark = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='reviewed_counseling_requests')
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.student.name} -> {self.hod_teacher.name}: {self.subject} ({self.status})"


class StudentDocument(models.Model):
    STATUS_PENDING = 'Pending'
    STATUS_VERIFIED = 'Verified'
    STATUS_REJECTED = 'Rejected'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_VERIFIED, 'Verified'),
        (STATUS_REJECTED, 'Rejected'),
    ]

    DOC_AADHAR = 'Aadhar Card'
    DOC_PAN = 'PAN Card'
    DOC_MARKSHEET = 'Marksheet'
    DOC_TRANSFER = 'Transfer Certificate'
    DOC_CASTE = 'Caste Certificate'
    DOC_INCOME = 'Income Certificate'
    DOC_OTHER = 'Other'
    DOCUMENT_TYPE_CHOICES = [
        (DOC_AADHAR, 'Aadhar Card'),
        (DOC_PAN, 'PAN Card'),
        (DOC_MARKSHEET, 'Marksheet'),
        (DOC_TRANSFER, 'Transfer Certificate'),
        (DOC_CASTE, 'Caste Certificate'),
        (DOC_INCOME, 'Income Certificate'),
        (DOC_OTHER, 'Other'),
    ]

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='documents')
    document_type = models.CharField(max_length=50, choices=DOCUMENT_TYPE_CHOICES, default=DOC_OTHER)
    title = models.CharField(max_length=150)
    document_file = models.FileField(upload_to='student_documents/')
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING)
    review_note = models.TextField(blank=True)
    reviewed_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='reviewed_student_documents',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    uploaded_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-uploaded_at']

    def __str__(self):
        return f"{self.student.name} - {self.title} ({self.status})"


class StudentDocumentShare(models.Model):
    student_document = models.ForeignKey(StudentDocument, on_delete=models.CASCADE, related_name='shares')
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='shared_documents')
    share_note = models.TextField(blank=True)
    is_active = models.BooleanField(default=True)
    shared_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('student_document', 'teacher')
        ordering = ['-shared_at']

    def __str__(self):
        return f"{self.student_document.title} -> {self.teacher.name}"


class SubjectAssignment(models.Model):
    teacher = models.ForeignKey(Teacher, on_delete=models.CASCADE, related_name='subject_assignments')
    subject = models.ForeignKey(Subject, on_delete=models.CASCADE, related_name='assignments')
    title = models.CharField(max_length=200)
    description = models.TextField(blank=True)
    assignment_file = models.FileField(upload_to='teacher_assignments/')
    due_date = models.DateField(null=True, blank=True)
    course = models.ForeignKey(Course, on_delete=models.CASCADE)
    department = models.ForeignKey(Dept, on_delete=models.CASCADE)
    session = models.ForeignKey(Session, on_delete=models.CASCADE)
    semester = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.title} ({self.subject.code})"


class AssignmentSubmission(models.Model):
    assignment = models.ForeignKey(SubjectAssignment, on_delete=models.CASCADE, related_name='submissions')
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='assignment_submissions')
    submission_file = models.FileField(upload_to='assignment_submissions/')
    submitted_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('assignment', 'student')
        ordering = ['-submitted_at']

    def __str__(self):
        return f"{self.student.name} -> {self.assignment.title}"


class AcademicProgressionLog(models.Model):
    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name='academic_progressions')
    from_semester = models.PositiveSmallIntegerField()
    to_semester = models.PositiveSmallIntegerField()
    from_session = models.ForeignKey(Session, on_delete=models.SET_NULL, null=True, blank=True, related_name='progressions_from')
    to_session = models.ForeignKey(Session, on_delete=models.SET_NULL, null=True, blank=True, related_name='progressions_to')
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        unique_together = (('student', 'from_semester', 'to_semester'),)

    def __str__(self):
        return f'{self.student.name}: Sem {self.from_semester} → {self.to_semester}'


class FeeStructure(models.Model):
    course = models.ForeignKey(Course, on_delete=models.CASCADE, related_name='fee_structures')
    department = models.ForeignKey(Dept, on_delete=models.CASCADE, related_name='fee_structures')
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='fee_structures')
    semester = models.PositiveSmallIntegerField(choices=Subject.SEMESTER_CHOICES)
    fee_head = models.CharField(max_length=100)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    due_date = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['course__name', 'department__name', 'semester', 'fee_head']
        unique_together = (('course', 'department', 'session', 'semester', 'fee_head'),)

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.course_id and not self.course.duration_years:
            raise ValidationError({'course': 'Configure the course duration before creating fee structures.'})
        if self.course_id and self.semester is not None and self.semester > min(8, int(self.course.duration_years) * 2):
            raise ValidationError({'semester': 'This semester is outside the configured course duration.'})
        if self.course_id and self.department_id:
            allowed = set(self.course.departments.values_list('id', flat=True))
            if self.course.dept_id:
                allowed.add(self.course.dept_id)
            if self.department_id not in allowed:
                raise ValidationError({'department': 'This department is not assigned to the selected course.'})

    def __str__(self):
        return f'{self.fee_head} - {self.course.shortname} / {self.department.name} / Sem {self.semester} / {self.session.name}'


class StudentFeeInvoice(models.Model):
    STATUS_PENDING = 'Pending'
    STATUS_PARTIAL = 'Partially Paid'
    STATUS_PAID = 'Paid'
    STATUS_CANCELLED = 'Cancelled'
    STATUS_CHOICES = [
        (STATUS_PENDING, 'Pending'),
        (STATUS_PARTIAL, 'Partially Paid'),
        (STATUS_PAID, 'Paid'),
        (STATUS_CANCELLED, 'Cancelled'),
    ]

    student = models.ForeignKey(Student, on_delete=models.PROTECT, related_name='fee_invoices')
    session = models.ForeignKey(Session, on_delete=models.PROTECT, related_name='fee_invoices')
    semester = models.PositiveSmallIntegerField(choices=Subject.SEMESTER_CHOICES)
    invoice_number = models.CharField(max_length=50, unique=True, blank=True)
    issue_date = models.DateField(default=timezone.localdate)
    due_date = models.DateField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal('0.00'))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_PENDING)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='created_fee_invoices')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-issue_date', '-id']
        unique_together = (('student', 'session', 'semester'),)

    def save(self, *args, **kwargs):
        if not self.invoice_number and self.student_id and self.session_id:
            self.invoice_number = f'FEE-{self.student_id:06d}-{self.session_id}-{self.semester:02d}'
        super().save(*args, **kwargs)

    @property
    def amount_paid(self):
        total = self.payments.filter(status=FeePayment.STATUS_SUCCESSFUL).aggregate(total=Sum('amount'))['total']
        return total or Decimal('0.00')

    @property
    def amount_due(self):
        return max(Decimal('0.00'), self.total_amount - self.amount_paid)

    def recalculate_status(self):
        if self.status == self.STATUS_CANCELLED:
            return self.status
        paid = self.amount_paid
        if paid >= self.total_amount and self.total_amount > 0:
            new_status = self.STATUS_PAID
        elif paid > 0:
            new_status = self.STATUS_PARTIAL
        else:
            new_status = self.STATUS_PENDING
        if self.status != new_status:
            self.status = new_status
            self.save(update_fields=['status', 'updated_at'])
        return self.status

    def __str__(self):
        return f'{self.invoice_number} - {self.student.name} ({self.status})'


class StudentFeeInvoiceItem(models.Model):
    invoice = models.ForeignKey(StudentFeeInvoice, on_delete=models.CASCADE, related_name='items')
    fee_head = models.CharField(max_length=100)
    description = models.CharField(max_length=255, blank=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])

    class Meta:
        ordering = ['fee_head']
        unique_together = (('invoice', 'fee_head'),)

    def __str__(self):
        return f'{self.fee_head}: ₹{self.amount}'


class FeePayment(models.Model):
    METHOD_RAZORPAY = 'Razorpay'
    METHOD_CASH = 'Cash'
    METHOD_UPI = 'UPI'
    METHOD_BANK = 'Bank Transfer'
    METHOD_CHEQUE = 'Cheque'
    METHOD_CHOICES = [
        (METHOD_RAZORPAY, 'Razorpay'),
        (METHOD_CASH, 'Cash'),
        (METHOD_UPI, 'UPI'),
        (METHOD_BANK, 'Bank Transfer'),
        (METHOD_CHEQUE, 'Cheque'),
    ]
    STATUS_CREATED = 'Created'
    STATUS_SUCCESSFUL = 'Successful'
    STATUS_FAILED = 'Failed'
    STATUS_REFUNDED = 'Refunded'
    STATUS_CHOICES = [
        (STATUS_CREATED, 'Created'),
        (STATUS_SUCCESSFUL, 'Successful'),
        (STATUS_FAILED, 'Failed'),
        (STATUS_REFUNDED, 'Refunded'),
    ]

    invoice = models.ForeignKey(StudentFeeInvoice, on_delete=models.PROTECT, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))])
    method = models.CharField(max_length=20, choices=METHOD_CHOICES)
    status = models.CharField(max_length=15, choices=STATUS_CHOICES, default=STATUS_CREATED)
    reference = models.CharField(max_length=128, blank=True, help_text='Bank/UPI/receipt reference for offline payments')
    gateway_order_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    gateway_payment_id = models.CharField(max_length=100, unique=True, null=True, blank=True)
    gateway_signature = models.CharField(max_length=128, blank=True)
    gateway_payload = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='recorded_fee_payments')
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['method', 'reference'],
                condition=~models.Q(reference=''),
                name='uniq_fee_payment_method_reference',
            ),
        ]

    def clean(self):
        from django.core.exceptions import ValidationError
        if self.amount is None or self.amount <= 0:
            raise ValidationError({'amount': 'Payment amount must be greater than zero.'})
        if self.method != self.METHOD_RAZORPAY and self.status == self.STATUS_SUCCESSFUL and not (self.reference or '').strip():
            raise ValidationError({'reference': 'A receipt/bank/UPI reference is required for an offline payment.'})
        if self.invoice_id and self.status == self.STATUS_SUCCESSFUL:
            if self.invoice.status == StudentFeeInvoice.STATUS_CANCELLED:
                raise ValidationError({'invoice': 'Payments cannot be applied to a cancelled invoice.'})
            paid = self.invoice.payments.filter(status=self.STATUS_SUCCESSFUL).exclude(pk=self.pk).aggregate(total=Sum('amount'))['total'] or Decimal('0.00')
            if paid + self.amount > self.invoice.total_amount:
                raise ValidationError({'amount': 'This payment exceeds the invoice balance.'})

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.invoice_id:
            self.invoice.recalculate_status()

    def __str__(self):
        return f'{self.invoice.invoice_number} - ₹{self.amount} ({self.status})'


post_save.connect(create_marks, sender=Student)
post_save.connect(create_marks, sender=Assign)
post_save.connect(create_marks_class, sender=Assign)

