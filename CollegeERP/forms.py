from django import forms
from info.models import SpecialUpdateNote, Teacher, Student, StudentCounselingRequest
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from info.models import FeeStructure, Subject

User = get_user_model()


class SpecialUpdateNoteForm(forms.ModelForm):
    class Meta:
        model = SpecialUpdateNote
        fields = ['title', 'pdf_file']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Enter title'}),
            'pdf_file': forms.ClearableFileInput(attrs={
                'class': 'form-control',
                'accept': '.pdf, application/pdf, application/vnd.ms-excel, application/vnd.openxmlformats-officedocument.spreadsheetml.sheet, application/msword, application/vnd.openxmlformats-officedocument.wordprocessingml.document, .xls, .xlsx, .doc, .docx'
            }),
        }
       
    def clean_pdf_file(self):
        file = self.cleaned_data.get('pdf_file')
        if not file:
            raise ValidationError("Please select a file.")
        allowed_mime_types = [
            'application/pdf',
            'application/vnd.ms-excel',
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            'application/msword',
            'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        ]
        if file.content_type not in allowed_mime_types:
            raise ValidationError("Unsupported file type.")
        return file

    def clean_title(self):
        title = self.cleaned_data.get('title')
        if not title or not title.strip():
            raise ValidationError("Title is required.")
        return title



class AdminProfileForm(forms.ModelForm):
    phone = forms.CharField(required=False, max_length=20, label='Phone')

    class Meta:
        model = User
        fields = ['first_name', 'last_name', 'email']


class TeacherProfileForm(forms.ModelForm):
    class Meta:
        model = Teacher
        fields = ['name', 'email', 'phone', 'sex', 'dob']

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if not email:
            return email
        exists = Teacher.objects.exclude(pk=self.instance.pk).filter(email__iexact=email).exists()
        if exists:
            raise ValidationError('This email is already used by another teacher.')
        return email


class StudentProfileForm(forms.ModelForm):
    class Meta:
        model = Student
        fields = ['name', 'email', 'phone', 'sex', 'dob']

    def clean_email(self):
        email = self.cleaned_data.get('email')
        if not email:
            return email
        exists = Student.objects.exclude(pk=self.instance.pk).filter(email__iexact=email).exists()
        if exists:
            raise ValidationError('This email is already used by another student.')
        return email


class StudentFamilyDetailsForm(forms.ModelForm):
    class Meta:
        model = Student
        fields = [
            'father_name',
            'mother_name',
            'guardian_name',
            'guardian_relation',
            'guardian_phone',
            'family_address',
            'family_city',
            'family_state',
            'family_pincode',
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field_name in self.fields:
            self.fields[field_name].required = True

    def clean_guardian_phone(self):
        phone = (self.cleaned_data.get('guardian_phone') or '').strip()
        if not phone.isdigit() or len(phone) != 10:
            raise ValidationError('Guardian phone must be a 10-digit number.')
        return phone

    def clean_family_pincode(self):
        pincode = (self.cleaned_data.get('family_pincode') or '').strip()
        if not pincode.isdigit() or len(pincode) != 6:
            raise ValidationError('Pincode must be a 6-digit number.')
        return pincode


class StudentCounselingRequestForm(forms.ModelForm):
    class Meta:
        model = StudentCounselingRequest
        fields = ['subject', 'concern', 'preferred_date']
        widgets = {
            'subject': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Short topic (e.g., stress, academics, family issue)'}),
            'concern': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'Describe your concern'}),
            'preferred_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        }

    def clean_subject(self):
        subject = (self.cleaned_data.get('subject') or '').strip()
        if not subject:
            raise ValidationError('Subject is required.')
        return subject

    def clean_concern(self):
        concern = (self.cleaned_data.get('concern') or '').strip()
        if len(concern) < 10:
            raise ValidationError('Please provide a little more detail (minimum 10 characters).')
        return concern





class FeeStructureForm(forms.ModelForm):
    class Meta:
        model = FeeStructure
        fields = ['course', 'department', 'session', 'semester', 'fee_head', 'amount', 'due_date', 'is_active']
        widgets = {
            'course': forms.Select(attrs={'class': 'form-control'}),
            'department': forms.Select(attrs={'class': 'form-control'}),
            'session': forms.Select(attrs={'class': 'form-control'}),
            'semester': forms.Select(attrs={'class': 'form-control'}),
            'fee_head': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Tuition Fee, Examination Fee, Library Fee'}),
            'amount': forms.NumberInput(attrs={'class': 'form-control', 'min': '0.01', 'step': '0.01'}),
            'due_date': forms.DateInput(attrs={'class': 'form-control', 'type': 'date'}),
        }

    def clean_fee_head(self):
        value = (self.cleaned_data.get('fee_head') or '').strip()
        if not value:
            raise ValidationError('Fee head is required.')
        return value

    def clean(self):
        cleaned = super().clean()
        course = cleaned.get('course')
        department = cleaned.get('department')
        if course and department:
            allowed_ids = set(course.departments.values_list('id', flat=True))
            if course.dept_id:
                allowed_ids.add(course.dept_id)
            if department.pk not in allowed_ids:
                self.add_error('department', 'This department is not assigned to the selected course.')
        if course and not course.duration_years:
            self.add_error('course', 'Configure the course duration before defining semester fees.')
        elif course and cleaned.get('semester'):
            max_semesters = min(8, int(course.duration_years) * 2)
            if cleaned['semester'] > max_semesters:
                self.add_error('semester', 'The semester is outside this course duration.')
        return cleaned
