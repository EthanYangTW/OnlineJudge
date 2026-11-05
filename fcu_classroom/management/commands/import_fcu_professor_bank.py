"""Create unpublished C practice drafts from a private, reviewed teacher bank."""

import re
import shutil
from datetime import datetime, timedelta, timezone as datetime_timezone

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from contest.models import Contest
from fcu_classroom.management.commands.import_fcu_c_bank import (
    MAX_CASE_BYTES, _plain_html, _read_json, _write_cases,
)
from fcu_classroom.models import Classroom, ClassroomQuestion, ClassroomQuiz
from problem.models import Problem, ProblemDifficulty, ProblemIOMode, ProblemRuleType
from utils.constants import ContestRuleType


PRACTICE_END = datetime(2100, 1, 1, tzinfo=datetime_timezone.utc)
SLUG = re.compile(r"[a-z0-9][a-z0-9-]{1,63}\Z")


def _validate_bank(raw):
    if len(raw) != 1 or not isinstance(raw[0], dict):
        raise CommandError("Expected one quiz object in the private bank")
    quiz = raw[0]
    questions = quiz.get("questions")
    if not isinstance(quiz.get("slug"), str) or not SLUG.fullmatch(quiz["slug"]):
        raise CommandError("Invalid quiz slug")
    if not isinstance(quiz.get("title"), str) or not 1 <= len(quiz["title"].strip()) <= 160:
        raise CommandError("Invalid quiz title")
    if not isinstance(questions, list) or not 1 <= len(questions) <= 100:
        raise CommandError("A quiz needs 1 to 100 C questions")
    slugs = set()
    for question in questions:
        if not isinstance(question, dict):
            raise CommandError("Question must be an object")
        slug = question.get("slug")
        if not isinstance(slug, str) or not SLUG.fullmatch(slug) or slug in slugs:
            raise CommandError("Invalid or duplicate C question slug")
        slugs.add(slug)
        for field, maximum in (("title", 160), ("description", 20000),
                               ("input", 10000), ("output", 10000)):
            value = question.get(field)
            if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
                raise CommandError("Invalid {} in {}".format(field, slug))
        tests = question.get("tests")
        if not isinstance(tests, list) or not 2 <= len(tests) <= 100:
            raise CommandError("{} needs 2 to 100 private cases".format(slug))
        for pair in tests:
            if (not isinstance(pair, list) or len(pair) != 2 or
                    not all(isinstance(value, str) and
                            len(value.encode("utf-8")) <= MAX_CASE_BYTES for value in pair)):
                raise CommandError("Invalid private case in {}".format(slug))
    return quiz


class Command(BaseCommand):
    help = "Import unpublished professor C drafts for one existing classroom."

    def add_arguments(self, parser):
        parser.add_argument("--class-slug", required=True)
        parser.add_argument("--bank", required=True, help="Private JSON with statements and test cases")
        parser.add_argument("--points", type=int, default=10)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not 1 <= options["points"] <= 1000:
            raise CommandError("Points must be between 1 and 1000")
        classroom = Classroom.objects.select_related("teacher").filter(slug=options["class_slug"]).first()
        if classroom is None:
            raise CommandError("Create the target classroom before importing")
        quiz_data = _validate_bank(_read_json(options["bank"], "professor bank"))
        slug = quiz_data["slug"]
        created_dirs = []
        try:
            with transaction.atomic():
                Classroom.objects.select_for_update().get(id=classroom.id)
                existing = ClassroomQuiz.objects.filter(classroom=classroom, slug=slug).first()
                if existing:
                    actual = {q.slug: q.kind for q in existing.questions.all()}
                    expected = {q["slug"]: ClassroomQuestion.C_LAB for q in quiz_data["questions"]}
                    if existing.mode != ClassroomQuiz.PRACTICE or actual != expected:
                        raise CommandError("An existing quiz has different questions; nothing changed")
                    self.stdout.write("Professor bank: already present; teacher edits preserved")
                    return
                if options["dry_run"]:
                    self.stdout.write("Professor bank: {} C questions; 1 new unpublished quiz (dry run)".format(
                        len(quiz_data["questions"])))
                    return
                start = timezone.now() - timedelta(minutes=1)
                contest = Contest.objects.create(
                    title=quiz_data["title"].strip(), description="", real_time_rank=False,
                    rule_type=ContestRuleType.OI, start_time=start, end_time=PRACTICE_END,
                    created_by=classroom.teacher, visible=True, allowed_ip_ranges=[])
                quiz = ClassroomQuiz.objects.create(
                    classroom=classroom, contest=contest, slug=slug,
                    title=quiz_data["title"].strip(), mode=ClassroomQuiz.PRACTICE,
                    published=False, starts_at=start, ends_at=None)
                for position, item in enumerate(quiz_data["questions"], 1):
                    case_id, case_scores, case_dir = _write_cases(item["tests"])
                    created_dirs.append(case_dir)
                    title = item["title"].strip()
                    problem = Problem.objects.create(
                        _id=item["slug"], contest=contest, title=title,
                        description=_plain_html(item["description"]),
                        input_description=_plain_html(item["input"]),
                        output_description=_plain_html(item["output"]),
                        samples=[{"input": item["tests"][0][0], "output": item["tests"][0][1]}],
                        test_case_id=case_id, test_case_score=case_scores, hint=None,
                        languages=["C"], template={}, created_by=classroom.teacher,
                        time_limit=2000, memory_limit=128,
                        io_mode={"io_mode": ProblemIOMode.standard,
                                 "input": "input.txt", "output": "output.txt"},
                        rule_type=ProblemRuleType.OI, difficulty=ProblemDifficulty.Low,
                        total_score=len(case_scores) * 10, visible=True,
                        source="FCU professor draft", share_submission=False)
                    ClassroomQuestion.objects.create(
                        quiz=quiz, problem=problem, slug=item["slug"], title=title,
                        kind=ClassroomQuestion.C_LAB, points=options["points"],
                        position=position, published=True, prompt="", choices=[], answer_index=None)
        except Exception:
            for path in created_dirs:
                shutil.rmtree(path, ignore_errors=True)
            raise
        self.stdout.write("Professor bank: {} C questions; 1 new unpublished quiz".format(
            len(quiz_data["questions"])))
