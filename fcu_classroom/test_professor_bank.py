import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from account.models import User
from fcu_classroom.models import Classroom, ClassroomQuestion, ClassroomQuiz
from problem.models import Problem


class ImportProfessorBankTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases = self.root / "test_case"
        self.cases.mkdir()
        settings_context = self.settings(TEST_CASE_DIR=str(self.cases))
        settings_context.enable()
        self.addCleanup(settings_context.disable)
        teacher = User.objects.create(username="teacher")
        self.classroom = Classroom.objects.create(slug="class-a", name="Class A", teacher=teacher)
        self.bank = self.root / "bank.json"
        self.write_bank()

    def write_bank(self, *, valid=True):
        self.bank.write_text(json.dumps([{
            "slug": "professor-drafts", "title": "Teacher drafts", "questions": [{
                "slug": "right-triangle", "title": "Right triangle",
                "description": "Decide whether three sides form a right triangle." if valid else "",
                "input": "Three integers.", "output": "Yes or No.",
                "tests": [["3 4 5\n", "Yes\n"], ["2 3 4\n", "No\n"]],
            }],
        }]), encoding="utf-8")

    def run_import(self, slug="class-a", **kwargs):
        return call_command("import_fcu_professor_bank", class_slug=slug,
                            bank=str(self.bank), **kwargs)

    def test_dry_run_then_private_unpublished_import(self):
        self.run_import(dry_run=True)
        self.assertFalse(ClassroomQuiz.objects.exists())
        self.assertFalse(list(self.cases.iterdir()))
        self.run_import()
        quiz = ClassroomQuiz.objects.get(classroom=self.classroom)
        self.assertFalse(quiz.published)
        self.assertEqual(quiz.mode, ClassroomQuiz.PRACTICE)
        question = ClassroomQuestion.objects.get(quiz=quiz)
        self.assertEqual(question.points, 10)
        self.assertEqual(question.problem.languages, ["C"])
        self.assertEqual(question.problem.template, {})
        self.assertFalse(question.problem.hint)
        self.assertEqual(question.problem.samples, [{"input": "3 4 5\n", "output": "Yes\n"}])
        self.assertEqual((self.cases / question.problem.test_case_id / "2.out").read_text(), "No\n")
        self.assertEqual(len(question.problem.test_case_score), 2)

    def test_idempotent_and_class_isolation(self):
        self.run_import()
        question = ClassroomQuestion.objects.get()
        question.points = 37
        question.save(update_fields=["points"])
        self.run_import()
        self.assertEqual(ClassroomQuestion.objects.get().points, 37)
        self.assertEqual(Problem.objects.count(), 1)
        other = Classroom.objects.create(slug="class-b", name="Class B",
                                         teacher=self.classroom.teacher)
        self.run_import(slug=other.slug)
        self.assertEqual(ClassroomQuiz.objects.count(), 2)
        self.assertEqual(Problem.objects.count(), 2)
        self.assertNotEqual(*[p.test_case_id for p in Problem.objects.order_by("id")])

    def test_invalid_bank_is_atomic(self):
        self.write_bank(valid=False)
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertFalse(ClassroomQuiz.objects.exists())
        self.assertFalse(list(self.cases.iterdir()))
