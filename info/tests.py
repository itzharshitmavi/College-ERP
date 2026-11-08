from decimal import Decimal

from django.test import TestCase
from django.core.exceptions import ValidationError
from django.utils import timezone

from info.models import (
    AcademicProgressionLog, Course, Dept, FeePayment, Session,
    Student, StudentFeeInvoice, StudentFeeInvoiceItem, Subject, User,
)


class AcademicLifecycleTests(TestCase):
    def setUp(self):
        self.department = Dept.objects.create(id="CSE", name="Computer Science")
        self.course = Course.objects.create(
            name="Bachelor of Technology", shortname="B.Tech",
            duration_years=4, dept=self.department,
        )
        self.session_2024 = Session.objects.create(name="2024-2025", is_current=True)
        self.session_2025 = Session.objects.create(name="2025-2026", is_current=False)
        self.user = User.objects.create_user(
            username="student-1", email="student@example.com", password="safe-password-123"
        )
        self.student = Student.objects.create(
            user=self.user, name="Test Student", roll=1001, email="student@example.com",
            dept=self.department, course=self.course, admission_year=2024,
            current_semester=1, current_session=self.session_2024, status="Approved",
        )

    def test_cohort_session_is_derived_from_admission_year_and_semester(self):
        self.assertEqual(self.student.session_for_semester(1).name, "2024-2025")
        self.assertEqual(self.student.session_for_semester(2).name, "2024-2025")
        self.assertEqual(self.student.session_for_semester(3).name, "2025-2026")
        self.assertEqual(self.student.session_for_semester(8).name, "2027-2028")

    def test_promotion_is_limited_to_active_academic_session(self):
        self.assertTrue(self.student.promote_semester())
        self.student.refresh_from_db()
        self.assertEqual(self.student.current_semester, 2)
        self.assertEqual(self.student.current_session, self.session_2024)
        self.assertFalse(self.student.promote_semester())
        self.student.refresh_from_db()
        self.assertEqual(self.student.current_semester, 2)

        self.session_2025.is_current = True
        self.session_2025.save()
        self.assertTrue(self.student.promote_semester())
        self.student.refresh_from_db()
        self.assertEqual(self.student.current_semester, 3)
        self.assertEqual(self.student.current_session, self.session_2025)
        self.assertEqual(AcademicProgressionLog.objects.filter(student=self.student).count(), 2)

    def test_subject_assignment_replaces_stale_subjects(self):
        semester_one = Subject.objects.create(
            name="Programming", code="CS101", semester=1,
            department=self.department, session=self.session_2024, course=self.course,
        )
        semester_two = Subject.objects.create(
            name="Data Structures", code="CS201", semester=2,
            department=self.department, session=self.session_2024, course=self.course,
        )
        self.student.subjects.add(semester_two)
        self.student.assign_current_subjects()
        self.assertQuerySetEqual(self.student.subjects.all(), [semester_one], transform=lambda x: x)

    def test_offline_fee_receipts_require_unique_references(self):
        invoice = StudentFeeInvoice.objects.create(
            student=self.student, session=self.session_2024, semester=1,
            total_amount=Decimal("12500.00"),
        )
        first = FeePayment.objects.create(
            invoice=invoice, amount=Decimal("100.00"),
            method=FeePayment.METHOD_UPI, status=FeePayment.STATUS_FAILED,
            reference="UPI-REFERENCE-001",
        )
        duplicate = FeePayment(
            invoice=invoice, amount=Decimal("100.00"),
            method=FeePayment.METHOD_UPI, status=FeePayment.STATUS_FAILED,
            reference="UPI-REFERENCE-001",
        )
        with self.assertRaises(ValidationError):
            duplicate.full_clean()

    def test_fee_invoice_is_reconciled_only_from_successful_payment(self):
        invoice = StudentFeeInvoice.objects.create(
            student=self.student, session=self.session_2024, semester=1,
            total_amount=Decimal("12500.00"),
        )
        StudentFeeInvoiceItem.objects.create(
            invoice=invoice, fee_head="Tuition Fee", amount=Decimal("12500.00")
        )
        pending = FeePayment.objects.create(
            invoice=invoice, amount=Decimal("12500.00"),
            method=FeePayment.METHOD_RAZORPAY, status=FeePayment.STATUS_CREATED,
        )
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, StudentFeeInvoice.STATUS_PENDING)

        pending.status = FeePayment.STATUS_SUCCESSFUL
        pending.gateway_payment_id = "pay_test_123"
        pending.verified_at = timezone.now()
        pending.full_clean()
        pending.save()
        invoice.refresh_from_db()
        self.assertEqual(invoice.status, StudentFeeInvoice.STATUS_PAID)
        self.assertEqual(invoice.amount_due, Decimal("0.00"))
