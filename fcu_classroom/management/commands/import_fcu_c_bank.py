"""Import the original FCU C practice bank without publishing its answer data."""

import hashlib
import html
import json
import os
import pwd
import grp
import re
import shutil
import uuid
from collections import defaultdict
from datetime import datetime, timedelta, timezone as datetime_timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from contest.models import Contest
from fcu_classroom.models import Classroom, ClassroomQuestion, ClassroomQuiz
from problem.models import Problem, ProblemRuleType, ProblemDifficulty, ProblemIOMode
from utils.constants import ContestRuleType


CHAPTERS = (2, 3, 4)
PRACTICE_END = datetime(2100, 1, 1, tzinfo=datetime_timezone.utc)
TITLE_NUMBER = re.compile(r"^\s*\d+\s*[.)]\s*")
MAX_CASE_BYTES = 1024 * 1024


def _read_json(path, label):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise CommandError("Cannot read {} JSON: {}".format(label, exc)) from exc
    if not isinstance(data, list):
        raise CommandError("{} JSON must be an array".format(label))
    return data


def _title(raw):
    result = TITLE_NUMBER.sub("", raw).strip()
    if not 1 <= len(result) <= 160:
        raise CommandError("Invalid problem title")
    return result


def _validate_bank(labs, choices):
    seen = set()
    for kind, entries in (("lab", labs), ("choice", choices)):
        for item in entries:
            if not isinstance(item, dict):
                raise CommandError("Bank entries must be objects")
            slug, title, chapter = item.get("slug"), item.get("title"), item.get("chapter")
            if (not isinstance(slug, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", slug) or
                    slug in seen or not isinstance(title, str) or chapter not in CHAPTERS):
                raise CommandError("Invalid or duplicate bank entry")
            seen.add(slug)
            _title(title)
            if kind == "lab":
                if not isinstance(item.get("description"), str) or not item["description"].strip():
                    raise CommandError("C lab needs a description")
                tests = item.get("tests")
                if not isinstance(tests, list) or not 2 <= len(tests) <= 100:
                    raise CommandError("C lab needs 2 to 100 test cases")
                for pair in tests:
                    if (not isinstance(pair, list) or len(pair) != 2 or
                            not all(isinstance(value, str) and len(value.encode("utf-8")) <= MAX_CASE_BYTES
                                    for value in pair)):
                        raise CommandError("Invalid C lab test case")
            else:
                options, answer = item.get("choices"), item.get("answerIndex")
                if (not isinstance(item.get("prompt"), str) or not item["prompt"].strip() or
                        not isinstance(item.get("code", ""), str) or
                        not isinstance(options, list) or not 2 <= len(options) <= 8 or
                        not all(isinstance(option, str) and 1 <= len(option.strip()) <= 500 for option in options) or
                        type(answer) is not int or not 0 <= answer < len(options)):
                    raise CommandError("Invalid C multiple-choice question")
    return seen


def _plain_html(text):
    return "<p>{}</p>".format(html.escape(text.strip()).replace("\n", "<br>"))


def _statement_sections(root):
    """Read only public input/output wording from the old statement files."""
    if root is None:
        return {}
    path = Path(root)
    if not path.is_dir():
        raise CommandError("Statements directory does not exist")
    sections = {}
    for metadata_path in path.glob("*/metadata.json"):
        statement_path = metadata_path.with_name("statement.md")
        if not statement_path.is_file():
            continue
        try:
            fcu_id = json.loads(metadata_path.read_text(encoding="utf-8")).get("fcuId")
            statement = statement_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError, ValueError) as exc:
            raise CommandError("Cannot read statement for {}".format(metadata_path.parent.name)) from exc
        if type(fcu_id) is not int:
            continue
        blocks = re.split(r"^##\s+(.+?)\s*$", statement, flags=re.MULTILINE)
        named = {blocks[index].strip().lower(): blocks[index + 1].strip()
                 for index in range(1, len(blocks) - 1, 2)}
        if "input" in named and "output" in named:
            sections[fcu_id] = (named["input"], named["output"])
    return sections


def _case_owner():
    try:
        return pwd.getpwnam("server").pw_uid, grp.getgrnam("spj").gr_gid
    except KeyError:
        return os.getuid(), os.getgid()


def _write_cases(tests):
    case_id = uuid.uuid4().hex
    case_dir = Path(settings.TEST_CASE_DIR) / case_id
    if not Path(settings.TEST_CASE_DIR).is_dir():
        raise CommandError("The private test-case volume is missing")
    case_dir.mkdir(mode=0o700)
    uid, gid = _case_owner()
    score_rows = []
    info = {"spj": False, "test_cases": {}}
    try:
        for index, (input_text, output_text) in enumerate(tests, 1):
            input_data, output_data = input_text.encode("utf-8"), output_text.encode("utf-8")
            input_name, output_name = "{}.in".format(index), "{}.out".format(index)
            (case_dir / input_name).write_bytes(input_data)
            (case_dir / output_name).write_bytes(output_data)
            info["test_cases"][str(index)] = {
                "stripped_output_md5": hashlib.md5(output_data.rstrip()).hexdigest(),
                "input_size": len(input_data), "output_size": len(output_data),
                "input_name": input_name, "output_name": output_name,
            }
            score_rows.append({"input_name": input_name, "output_name": output_name, "score": 10})
        (case_dir / "info").write_text(json.dumps(info), encoding="utf-8")
        for item in case_dir.iterdir():
            if os.geteuid() == 0:
                os.chown(item, uid, gid)
            item.chmod(0o640)
        if os.geteuid() == 0:
            os.chown(case_dir, uid, gid)
        case_dir.chmod(0o710)
        return case_id, score_rows, case_dir
    except Exception:
        shutil.rmtree(case_dir, ignore_errors=True)
        raise


def _difficulty(raw):
    return {"Easy": ProblemDifficulty.Low, "Medium": ProblemDifficulty.Mid,
            "Hard": ProblemDifficulty.High}.get(raw, ProblemDifficulty.Low)


def _expected_by_group(labs, choices):
    grouped = defaultdict(list)
    for item in choices:
        grouped[(item["chapter"], ClassroomQuestion.MCQ)].append(item)
    for item in labs:
        grouped[(item["chapter"], ClassroomQuestion.C_LAB)].append(item)
    return grouped


def _quiz_slug(chapter, kind):
    return "chapter-{}-{}".format(chapter, "quiz" if kind == ClassroomQuestion.MCQ else "c-lab")


def _quiz_title(chapter, kind):
    return "Chapter {} · {}".format(chapter, "C Quiz" if kind == ClassroomQuestion.MCQ else "C Lab")


class Command(BaseCommand):
    help = "Import the private FCU C bank as unpublished practice quizzes in one classroom."

    def add_arguments(self, parser):
        parser.add_argument("--class-slug", required=True)
        parser.add_argument("--labs", required=True, help="Path to private cLabs.json")
        parser.add_argument("--choices", required=True, help="Path to private cQuizzes.json")
        parser.add_argument("--statements-dir", help="Optional old problems/ directory for public input/output wording")
        parser.add_argument("--lab-points", type=int, default=10)
        parser.add_argument("--choice-points", type=int, default=5)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        if not 1 <= options["lab_points"] <= 1000 or not 1 <= options["choice_points"] <= 1000:
            raise CommandError("Points must be between 1 and 1000")
        classroom = Classroom.objects.select_related("teacher").filter(slug=options["class_slug"]).first()
        if classroom is None:
            raise CommandError("Create the target classroom before importing the bank")
        labs = _read_json(options["labs"], "C labs")
        choices = _read_json(options["choices"], "C quizzes")
        _validate_bank(labs, choices)
        statement_sections = _statement_sections(options["statements_dir"])
        groups = _expected_by_group(labs, choices)
        if not groups:
            raise CommandError("Bank is empty")
        if any(key[0] not in CHAPTERS for key in groups):
            raise CommandError("Unsupported chapter")

        created_dirs = []
        created_quizzes = 0
        skipped_quizzes = 0
        try:
            with transaction.atomic():
                Classroom.objects.select_for_update().get(id=classroom.id)
                for (chapter, kind), entries in sorted(groups.items()):
                    slug = _quiz_slug(chapter, kind)
                    existing = ClassroomQuiz.objects.filter(classroom=classroom, slug=slug).first()
                    if existing:
                        actual = {q.slug: q.kind for q in existing.questions.all()}
                        expected = {item["slug"]: kind for item in entries}
                        if existing.mode != ClassroomQuiz.PRACTICE or actual != expected:
                            raise CommandError("Quiz {} already exists with different questions; nothing changed".format(slug))
                        skipped_quizzes += 1
                        continue
                    created_quizzes += 1
                    if options["dry_run"]:
                        continue
                    start = timezone.now() - timedelta(minutes=1)
                    title = _quiz_title(chapter, kind)
                    contest = Contest.objects.create(title=title, description="", real_time_rank=False,
                                                     rule_type=ContestRuleType.OI, start_time=start,
                                                     end_time=PRACTICE_END, created_by=classroom.teacher,
                                                     visible=True, allowed_ip_ranges=[])
                    quiz = ClassroomQuiz.objects.create(classroom=classroom, contest=contest, slug=slug,
                                                        title=title, mode=ClassroomQuiz.PRACTICE,
                                                        published=False, starts_at=start, ends_at=None)
                    for position, item in enumerate(entries, 1):
                        clean_title = _title(item["title"])
                        if kind == ClassroomQuestion.MCQ:
                            prompt = item["prompt"].strip()
                            code = item.get("code", "").strip()
                            if code:
                                prompt += "\n\n" + code
                            ClassroomQuestion.objects.create(quiz=quiz, slug=item["slug"], title=clean_title,
                                                             kind=kind, points=options["choice_points"],
                                                             position=position, published=True,
                                                             prompt=prompt, choices=item["choices"],
                                                             answer_index=item["answerIndex"])
                            continue
                        case_id, case_scores, case_dir = _write_cases(item["tests"])
                        created_dirs.append(case_dir)
                        input_text, output_text = statement_sections.get(
                            item.get("id"), ("Read the values described in the task from standard input.",
                                             "Print the requested result to standard output."))
                        # The first original case becomes a public sample. Other cases stay in /data/test_case.
                        problem = Problem.objects.create(
                            _id=item["slug"], contest=contest, title=clean_title,
                            description=_plain_html(item["description"]),
                            input_description=_plain_html(input_text), output_description=_plain_html(output_text),
                            samples=[{"input": item["tests"][0][0], "output": item["tests"][0][1]}],
                            test_case_id=case_id, test_case_score=case_scores, hint=None,
                            languages=["C"], template={}, created_by=classroom.teacher,
                            time_limit=2000, memory_limit=128,
                            io_mode={"io_mode": ProblemIOMode.standard, "input": "input.txt", "output": "output.txt"},
                            rule_type=ProblemRuleType.OI, difficulty=_difficulty(item.get("difficulty")),
                            total_score=len(case_scores) * 10, visible=True,
                            source="FCU C Chapter {}".format(chapter), share_submission=False)
                        ClassroomQuestion.objects.create(quiz=quiz, problem=problem, slug=item["slug"],
                                                         title=clean_title, kind=kind, points=options["lab_points"],
                                                         position=position, published=True, prompt="",
                                                         choices=[], answer_index=None)
                if options["dry_run"]:
                    transaction.set_rollback(True)
        except Exception:
            for path in created_dirs:
                shutil.rmtree(path, ignore_errors=True)
            raise
        self.stdout.write("C bank: {} labs, {} choices; {} new unpublished quizzes, {} already present{}".format(
            len(labs), len(choices), created_quizzes, skipped_quizzes,
            " (dry run)" if options["dry_run"] else ""))
