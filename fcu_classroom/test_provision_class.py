import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from account.models import AdminType, User
from fcu_classroom.models import Classroom, Enrollment


class ProvisionFCUClassTest(TestCase):
    def setUp(self):
        self.teacher = User.objects.create(username="teacher", admin_type=AdminType.SUPER_ADMIN)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.roster = Path(self.temp.name) / "roster.csv"
        self.roster.write_text("login,name,class,password\nD1111111,Alice,A,D1111111\n", encoding="utf-8")

    def run_import(self, **kwargs):
        return call_command("provision_fcu_class", class_slug="class-a", class_name="Class A",
                            roster=str(self.roster), teacher="teacher", **kwargs)

    def test_import_is_hashed_and_idempotent(self):
        self.run_import()
        self.run_import()
        self.assertEqual(Classroom.objects.count(), 1)
        self.assertEqual(Enrollment.objects.count(), 1)
        student = User.objects.get(username="D1111111")
        self.assertTrue(student.check_password("D1111111"))
        self.assertNotEqual(student.password, "D1111111")

    def test_invalid_roster_rolls_back_new_class(self):
        self.roster.write_text("login,name,class,password\nD1111111,Alice,A,bad\n", encoding="utf-8")
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertFalse(Classroom.objects.exists())
