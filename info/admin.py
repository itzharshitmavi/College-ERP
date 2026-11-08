from datetime import timedelta, datetime

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.http import HttpResponseRedirect
from django.urls import path
from .models import PendingTeacher, PendingStudent, RejectedTeacher, RejectedStudent
from .models import Dept, Class, Student, Course, Teacher, Assign, AssignTime
from .models import StudentCourse, Marks, User, AttendanceRange, AttendanceRecord, StudentAttendance, AttendanceSummary
from .models import HODAssignment
from .models import StudentDocument
from .models import StudentCounselingRequest
from .models import AcademicProgressionLog, FeeStructure, StudentFeeInvoice, StudentFeeInvoiceItem, FeePayment

from django.contrib import admin
from django.contrib import messages
from django.db import transaction
from .models import PendingTeacher, PendingStudent,SpecialUpdateNote

# Register your models here.

days = {
    'Monday': 1,
    'Tuesday': 2,
    'Wednesday': 3,
    'Thursday': 4,
    'Friday': 5,
    'Saturday': 6,
}


@admin.action(description="Archive selected teacher requests as rejected")
def reject_teachers(modeladmin, request, queryset):
    count = 0
    for obj in queryset.select_related('dept', 'course', 'user'):
        with transaction.atomic():
            rejected = RejectedTeacher.objects.create(
                username=obj.username,
                name=obj.name,
                email=obj.email,
                dept=obj.dept,
                user=obj.user,
                phone=obj.phone,
                password=obj.password,
                dob=obj.dob,
                sex=obj.sex,
                photo=obj.photo,
                course=obj.course,
            )
            obj.user = None
            obj.save(update_fields=['user'])
            obj.delete()
            count += 1
    messages.success(request, f"{count} teacher request(s) archived as rejected.")


@admin.action(description="Archive selected student requests as rejected")
def reject_students(modeladmin, request, queryset):
    count = 0
    for obj in queryset.select_related('department', 'course', 'user'):
        with transaction.atomic():
            rejected = RejectedStudent.objects.create(
                name=obj.name,
                email=obj.email,
                department=obj.department,
                roll=obj.roll,
                phone=obj.phone,
                sex=obj.sex,
                password=obj.password,
                dob=obj.dob,
                user=obj.user,
                photo=obj.photo,
                course=obj.course,
                admission_year=obj.admission_year,
                current_semester=obj.current_semester,
                current_session=obj.current_session,
            )
            rejected.sessions.set(obj.sessions.all())
            obj.user = None
            obj.save(update_fields=['user'])
            obj.delete()
            count += 1
    messages.success(request, f"{count} student request(s) archived as rejected.")


def daterange(start_date, end_date):
    for n in range(int((end_date - start_date).days)):
        yield start_date + timedelta(n)


class ClassInline(admin.TabularInline):
    model = Class
    extra = 0


class DeptAdmin(admin.ModelAdmin):
    inlines = [ClassInline]
    list_display = ('name', 'id')
    search_fields = ('name', 'id')
    ordering = ['name']


class StudentInline(admin.TabularInline):
    model = Student
    extra = 0


class ClassAdmin(admin.ModelAdmin):
    list_display = ('id', 'dept', 'section')
    search_fields = ('id', 'dept__name','section')
    ordering = ['dept__name', 'section']
    # inlines = [StudentInline]


class CourseAdmin(admin.ModelAdmin):
    list_display = ('id', 'name', 'dept')
    search_fields = ('id', 'name', 'dept__name')
    ordering = ['dept', 'id']


class AssignTimeInline(admin.TabularInline):
    model = AssignTime
    extra = 0


class AssignAdmin(admin.ModelAdmin):
    inlines = [AssignTimeInline]
    list_display = ('session', 'course', 'teacher')
    search_fields = ('session__name', 'course__name', 'teacher__name', 'course__shortname')
    ordering = ['session__name', 'course__name', 'teacher__name']
    raw_id_fields = ['session', 'course', 'teacher']


class MarksInline(admin.TabularInline):
    model = Marks
    extra = 0


class StudentCourseAdmin(admin.ModelAdmin):
    inlines = [MarksInline]
    list_display = ('student', 'course',)
    search_fields = ('student__name', 'student__email', 'student__roll', 'course__name')
    ordering = ('student__current_session__name', 'student__roll', 'student__name')


class StudentAdmin(admin.ModelAdmin):
    list_display = ['name', 'roll', 'course', 'dept', 'admission_year', 'current_session', 'current_semester', 'academic_status', 'status']
    list_filter = ('academic_status', 'status', 'course', 'dept', 'current_semester', 'current_session')
    ordering = ['name', 'roll', 'admission_year']
    search_fields = ('name', 'email', '=roll', 'course__name', 'dept__name')
    list_select_related = ('course', 'dept', 'current_session')


class PendingTeacherAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'dept', 'course', 'phone')
    list_filter = ('dept', 'course')
    search_fields = ('name', 'email', 'username', 'phone')
    ordering = ('name',)
    actions = (reject_teachers,)


class PendingStudentAdmin(admin.ModelAdmin):
    list_display = ('name', 'roll', 'email', 'department', 'course', 'admission_year', 'current_semester')
    list_filter = ('department', 'course', 'admission_year', 'current_semester')
    search_fields = ('name', 'email', '=roll', 'course__name', 'department__name')
    ordering = ('admission_year', 'roll', 'name')
    actions = (reject_students,)


class TeacherAdmin(admin.ModelAdmin):
    list_display = ('name', 'dept')
    search_fields = ('name', 'dept__name')
    ordering = ['dept__name', 'name']


class HODAssignmentAdmin(admin.ModelAdmin):
    list_display = ('teacher', 'course', 'department', 'year', 'is_active')
    list_filter = ('course', 'department', 'year', 'is_active')
    search_fields = ('teacher__name', 'course__name', 'department__name')
    ordering = ['course__name', 'department__name', 'year', 'teacher__name']


class StudentDocumentAdmin(admin.ModelAdmin):
    list_display = ('student', 'document_type', 'title', 'status', 'reviewed_by', 'uploaded_at')
    list_filter = ('status', 'document_type', 'uploaded_at')
    search_fields = ('student__name', 'student__roll', 'title')
    ordering = ['-uploaded_at']


class StudentCounselingRequestAdmin(admin.ModelAdmin):
    list_display = ('student', 'hod_teacher', 'subject', 'status', 'reviewed_by', 'created_at')
    list_filter = ('status', 'created_at')
    search_fields = ('student__name', 'student__roll', 'subject', 'hod_teacher__name')
    ordering = ['-created_at']


class FeeStructureAdmin(admin.ModelAdmin):
    list_display = ('fee_head', 'course', 'department', 'session', 'semester', 'amount', 'due_date', 'is_active')
    list_filter = ('course', 'department', 'session', 'semester', 'is_active')
    search_fields = ('fee_head', 'course__name', 'department__name', 'session__name')
    ordering = ('course__name', 'department__name', 'session__name', 'semester', 'fee_head')


class FeeInvoiceItemInline(admin.TabularInline):
    model = StudentFeeInvoiceItem
    extra = 0
    readonly_fields = ('fee_head', 'description', 'amount')
    can_delete = False


class FeePaymentInline(admin.TabularInline):
    model = FeePayment
    extra = 0
    fields = ('amount', 'method', 'status', 'reference', 'gateway_order_id', 'gateway_payment_id', 'verified_at', 'created_at', 'created_by')
    readonly_fields = fields
    can_delete = False
    max_num = 0


class StudentFeeInvoiceAdmin(admin.ModelAdmin):
    list_display = ('invoice_number', 'student', 'session', 'semester', 'total_amount', 'status', 'issue_date', 'due_date')
    list_filter = ('status', 'session', 'semester', 'student__course', 'student__dept')
    search_fields = ('invoice_number', 'student__name', 'student__email', '=student__roll')
    readonly_fields = ('student', 'session', 'semester', 'invoice_number', 'issue_date', 'total_amount', 'status', 'created_by', 'created_at', 'updated_at')
    inlines = (FeeInvoiceItemInline, FeePaymentInline)
    list_select_related = ('student', 'session', 'student__course', 'student__dept')

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class FeePaymentAdmin(admin.ModelAdmin):
    list_display = ('invoice', 'amount', 'method', 'status', 'reference', 'gateway_payment_id', 'created_at')
    list_filter = ('method', 'status', 'created_at')
    search_fields = ('invoice__invoice_number', 'invoice__student__name', 'reference', 'gateway_order_id', 'gateway_payment_id')
    readonly_fields = ('gateway_order_id', 'gateway_payment_id', 'gateway_signature', 'gateway_payload', 'verified_at', 'created_at')
    list_select_related = ('invoice', 'invoice__student')

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class AcademicProgressionLogAdmin(admin.ModelAdmin):
    list_display = ('student', 'from_semester', 'to_semester', 'from_session', 'to_session', 'performed_by', 'created_at')
    list_filter = ('from_semester', 'to_semester', 'created_at')
    search_fields = ('student__name', 'student__email', '=student__roll')
    readonly_fields = ('student', 'from_semester', 'to_semester', 'from_session', 'to_session', 'performed_by', 'created_at')


# class NoCacheMiddleware:
#     def __init__(self, get_response):
#         self.get_response = get_response

#     def __call__(self, request):
#         response = self.get_response(request)
#         response['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
#         response['Pragma'] = 'no-cache'
#         response['Expires'] = '0'
#         return response


@admin.register(SpecialUpdateNote)
class SpecialUpdateNoteAdmin(admin.ModelAdmin):
    list_display = ('title', 'uploaded_at')
    readonly_fields = ('uploaded_at',)
    search_fields = ('title',)


admin.site.register(User, UserAdmin)
admin.site.register(Dept, DeptAdmin)
admin.site.register(Class, ClassAdmin)
admin.site.register(Student, StudentAdmin)
admin.site.register(Course, CourseAdmin)
admin.site.register(Teacher, TeacherAdmin)
admin.site.register(HODAssignment, HODAssignmentAdmin)
admin.site.register(StudentDocument, StudentDocumentAdmin)
admin.site.register(StudentCounselingRequest, StudentCounselingRequestAdmin)
admin.site.register(Assign, AssignAdmin)
admin.site.register(StudentCourse, StudentCourseAdmin)
admin.site.register(AttendanceRecord)
admin.site.register(StudentAttendance)
admin.site.register(AttendanceSummary)
admin.site.register(PendingTeacher, PendingTeacherAdmin)
admin.site.register(PendingStudent, PendingStudentAdmin)
admin.site.register(RejectedTeacher)
admin.site.register(RejectedStudent)


admin.site.register(AcademicProgressionLog, AcademicProgressionLogAdmin)
admin.site.register(FeeStructure, FeeStructureAdmin)
admin.site.register(StudentFeeInvoice, StudentFeeInvoiceAdmin)
admin.site.register(FeePayment, FeePaymentAdmin)
