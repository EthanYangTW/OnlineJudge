import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from account.models import User
from fcu_classroom.models import Classroom, ClassroomQuestion, ClassroomQuiz
from problem.models import Problem


class ImportFCUCBankTest(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cases = self.root / "test_case"
        self.cases.mkdir()
        self.settings_context = self.settings(TEST_CASE_DIR=str(self.cases))
        self.settings_context.enable()
        self.addCleanup(self.settings_context.disable)
        teacher = User.objects.create(username="teacher")
        self.classroom = Classroom.objects.create(slug="class-a", name="Class A", teacher=teacher)
        self.labs = self.root / "cLabs.json"
        self.choices = self.root / "cQuizzes.json"
        self.write_bank()

    def write_bank(self, valid=True):
        self.labs.write_text(json.dumps([{
            "id": 46, "slug": "fcu-c-lab-add-two", "title": "1. Add Two Numbers",
            "chapter": 2, "difficulty": "Easy", "description": "Print the sum of two integers.",
            "hint": "PRIVATE HINT", "starter": "PRIVATE STARTER",
            "tests": [["2 3\n", "5\n"], ["-1 2\n", "1\n"]],
        }]), encoding="utf-8")
        self.choices.write_text(json.dumps([{
            "slug": "fcu-c-printf", "title": "1. Printf", "chapter": 2,
            "prompt": "What does this print?" if valid else "", "code": 'printf("A");',
            "choices": ["A", "B", "C", "D"], "answerIndex": 0,
            "explanation": "PRIVATE EXPLANATION",
        }]), encoding="utf-8")

    def run_import(self, **kwargs):
        return call_command("import_fcu_c_bank", class_slug=self.classroom.slug,
                            labs=str(self.labs), choices=str(self.choices), **kwargs)

    def test_import_is_unpublished_and_omits_private_content(self):
        self.run_import()
        self.assertEqual(ClassroomQuiz.objects.filter(classroom=self.classroom).count(), 2)
        self.assertFalse(ClassroomQuiz.objects.filter(classroom=self.classroom, published=True).exists())
        self.assertEqual(ClassroomQuestion.objects.filter(quiz__classroom=self.classroom).count(), 2)
        lab = ClassroomQuestion.objects.get(kind=ClassroomQuestion.C_LAB)
        choice = ClassroomQuestion.objects.get(kind=ClassroomQuestion.MCQ)
        self.assertEqual(lab.title, "Add Two Numbers")
        self.assertEqual(choice.title, "Printf")
        self.assertEqual(choice.answer_index, 0)
        self.assertNotIn("PRIVATE", choice.prompt)
        self.assertEqual(lab.problem.languages, ["C"])
        self.assertEqual(lab.problem.template, {})
        self.assertEqual(lab.problem.hint, "")
        self.assertEqual(lab.problem.samples, [{"input": "2 3\n", "output": "5\n"}])
        self.assertEqual(len(lab.problem.test_case_score), 2)
        self.assertEqual(lab.problem.total_score, 20)
        case_dir = self.cases / lab.problem.test_case_id
        self.assertEqual((case_dir / "2.out").read_text(), "1\n")
        info = json.loads((case_dir / "info").read_text())
        self.assertEqual(info["test_cases"]["2"]["output_name"], "2.out")
        self.assertEqual(len(info["test_cases"]), 2)
        self.assertEqual(case_dir.stat().st_mode & 0o777, 0o710)

    def test_rerun_does_not_change_teacher_edits(self):
        self.run_import()
        lab = ClassroomQuestion.objects.get(kind=ClassroomQuestion.C_LAB)
        original_id = lab.problem_id
        lab.problem.title = "Teacher edited title"
        lab.problem.save(update_fields=["title"])
        self.run_import()
        self.assertEqual(Problem.objects.count(), 1)
        self.assertEqual(ClassroomQuestion.objects.get(kind=ClassroomQuestion.C_LAB).problem_id, original_id)
        self.assertEqual(Problem.objects.get(id=original_id).title, "Teacher edited title")
        self.assertEqual(len(list(self.cases.iterdir())), 1)

    def test_invalid_bank_and_dry_run_make_no_changes(self):
        self.run_import(dry_run=True)
        self.assertFalse(ClassroomQuiz.objects.exists())
        self.assertFalse(list(self.cases.iterdir()))
        self.write_bank(valid=False)
        with self.assertRaises(CommandError):
            self.run_import()
        self.assertFalse(ClassroomQuiz.objects.exists())
        self.assertFalse(list(self.cases.iterdir()))

    def test_same_bank_can_be_imported_for_separate_classes(self):
        teacher = self.classroom.teacher
        other_class = Classroom.objects.create(slug="class-b", name="Class B", teacher=teacher)
        self.run_import()
        call_command("import_fcu_c_bank", class_slug=other_class.slug,
                     labs=str(self.labs), choices=str(self.choices))
        self.assertEqual(ClassroomQuiz.objects.filter(classroom=self.classroom).count(), 2)
        self.assertEqual(ClassroomQuiz.objects.filter(classroom=other_class).count(), 2)
        self.assertEqual(Problem.objects.count(), 2)
        self.assertNotEqual(*[question.problem.test_case_id for question in
                              ClassroomQuestion.objects.filter(kind=ClassroomQuestion.C_LAB)
                              .select_related("problem")])
