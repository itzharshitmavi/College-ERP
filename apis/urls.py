from django.urls import path, include
import apis.views as api_view
from django.contrib import admin


urlpatterns = [
    path('details/', api_view.DetailView.as_view()),
    path('attendance/', api_view.AttendanceView.as_view()),
    path('marks/', api_view.MarksView.as_view()),
    path('timetable/', api_view.TimetableView.as_view()),
    path('teacher/attendance/history/', api_view.TeacherAttendanceHistoryView.as_view()),
    path('meta/registration/', api_view.RegistrationMetaView.as_view()),
    path('register/student/', api_view.StudentRegistrationRequestView.as_view()),
    path('register/teacher/', api_view.TeacherRegistrationRequestView.as_view()),
    path('teacher/subjects/', api_view.TeacherSubjectsManageView.as_view()),
    path('teacher/subjects/<int:subject_id>/', api_view.TeacherSubjectDeleteView.as_view()),
]
