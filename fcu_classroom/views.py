import csv
import hashlib
import io
import os
import re
from datetime import datetime, timezone as py_timezone
from urllib.parse import urljoin

import requests
from otpauth import OtpAuth
from django.contrib import auth
from django.contrib.sessions.backends.cache import SessionStore
from django.conf import settings
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models.functions import Coalesce
from django.http import JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views import View

from account.models import AdminType, User, UserProfile
from contest.models import Contest
from options.options import SysOptions
from problem.models import Problem, ProblemRuleType
from submission.models import JudgeStatus
from utils.constants import ContestRuleType

from .access import (classroom_access, clear_login_failures, client_ip,
                     is_classroom_owner, is_classroom_teacher, login_is_limited,
                     record_login_failure)
from .models import (Classroom, ClassroomAssistant, ClassroomAttempt, ClassroomDraft, ClassroomQuestion,
                     ClassroomQuiz, ClassroomScore, Enrollment)
from .signals import c_earned, refresh_score


LOGIN_RE = re.compile(r"^[A-Za-z0-9_.-]{2,64}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
PRACTICE_END = datetime(2100, 1, 1, tzinfo=py_timezone.utc)


def respond(data=None, error=None, status=200):
    response = JsonResponse({"error": error, "data": data}, status=status)
    response["Cache-Control"] = "private, no-store"
    return response


def fail(message, status=400):
    return respond(data=message, error="fcu-error", status=status)


def body_data(request):
    if not request.content_type.startswith("application/json") or len(request.body) > 262144:
        raise ValueError("JSON request must be at most 256 KiB")
    import json
    try:
        data = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Invalid JSON")
    if not isinstance(data, dict):
        raise ValueError("JSON object required")
    return data


def parse_time(raw, required=False):
    if raw is None or raw == "":
        if required:
            raise ValueError("A time is required")
        return None
    if not isinstance(raw, str):
        raise ValueError("Invalid time")
    value = parse_datetime(raw)
    if value is None or timezone.is_naive(value):
        raise ValueError("Time must be ISO 8601 with a timezone")
    return value


def get_class(request, slug):
    classroom = Classroom.objects.select_related("teacher").filter(slug=slug).first()
    if classroom is None:
        return None
    return classroom if classroom_access(request.user, classroom, client_ip(request)) else None


def get_quiz(request, slug, quiz_id):
    classroom = get_class(request, slug)
    if classroom is None:
        return None, None
    return classroom, ClassroomQuiz.objects.select_related("contest").filter(classroom=classroom, id=quiz_id).first()


def open_for_student(quiz, now=None):
    now = now or timezone.now()
    return quiz.published and quiz.starts_at <= now and (quiz.ends_at is None or now < quiz.ends_at)


def visible_to_student(quiz, now=None):
    return quiz.published and quiz.starts_at <= (now or timezone.now())


def class_json(classroom, actor=None):
    role = ("owner" if actor and is_classroom_owner(actor, classroom) else
            "assistant" if actor and is_classroom_teacher(actor, classroom) else "student")
    return {"slug": classroom.slug, "name": classroom.name,
            "ipLockEnabled": classroom.ip_lock_enabled, "role": role}


def quiz_json(quiz, teacher=False):
    return {"id": quiz.id, "slug": quiz.slug, "title": quiz.title, "mode": quiz.mode,
            "published": quiz.published if teacher else True, "contestId": quiz.contest_id,
            "startsAt": quiz.starts_at.isoformat(),
            "endsAt": quiz.ends_at.isoformat() if quiz.ends_at else None,
            "open": open_for_student(quiz)}


def question_json(question, teacher=False):
    data = {"id": question.id, "slug": question.slug, "title": question.title,
            "prompt": question.prompt, "kind": question.kind, "points": question.points,
            "position": question.position, "published": question.published,
            "problemId": question.problem_id,
            "problemDisplayId": question.problem._id if question.problem_id else None,
            "choices": question.choices if question.kind == ClassroomQuestion.MCQ else []}
    if teacher and question.kind == ClassroomQuestion.MCQ:
        data["answerIndex"] = question.answer_index
    return data


def revoke_sessions(user):
    for key in user.session_keys or []:
        if key:
            SessionStore().delete(key)
    user.session_keys = []
    user.save(update_fields=["session_keys"])


class ClassroomView(View):
    def get(self, request):
        if not request.user.is_authenticated:
            return fail("Please sign in", 401)
        if request.user.is_super_admin():
            classes = Classroom.objects.all()
        else:
            classes = Classroom.objects.filter(teacher=request.user) | Classroom.objects.filter(
                enrollments__user=request.user) | Classroom.objects.filter(assistants__user=request.user)
        data = [class_json(item, request.user) for item in classes.distinct().order_by("slug")
                if classroom_access(request.user, item, client_ip(request))]
        return respond(data)

    def post(self, request):
        if not request.user.is_authenticated or request.user.is_disabled or not request.user.is_admin_role():
            return fail("Teacher only", 403)
        try:
            data = body_data(request)
            slug, name = data.get("slug"), data.get("name")
            if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
                raise ValueError("Invalid class ID")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
                raise ValueError("Invalid class name")
            classroom = Classroom.objects.create(slug=slug, name=name.strip(), teacher=request.user)
        except IntegrityError:
            return fail("Class ID already exists", 409)
        except ValueError as exc:
            return fail(str(exc))
        return respond(class_json(classroom, request.user), status=201)


class ClassroomDetailView(View):
    def get(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None:
            return fail("Class unavailable", 404)
        return respond(class_json(classroom, request.user))

    def put(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None or not is_classroom_teacher(request.user, classroom):
            return fail("Teacher only", 403)
        try:
            data = body_data(request)
            name = data.get("name")
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
                raise ValueError("Invalid class name")
            classroom.name = name.strip()
            classroom.save(update_fields=["name"])
        except ValueError as exc:
            return fail(str(exc))
        return respond(class_json(classroom, request.user))


class ClassroomLoginView(View):
    def post(self, request):
        try:
            data = body_data(request)
            slug, login, password = data.get("class"), data.get("login"), data.get("password")
            if not all(isinstance(x, str) for x in (login, password)) or (slug is not None and not isinstance(slug, str)):
                raise ValueError("Invalid credentials")
            if len(login) > 64 or len(password) > 128:
                raise ValueError("Invalid credentials")
            remote = client_ip(request)
            if remote is None:
                return fail("Client address unavailable", 503)
            if login_is_limited(request, login):
                return fail("Too many attempts; try again later", 429)
            user = auth.authenticate(request, username=login, password=password)
            if slug:
                classroom = Classroom.objects.filter(slug=slug).first()
            elif user is not None:
                enrolled = list(Enrollment.objects.filter(user=user).select_related("classroom")[:2])
                classroom = enrolled[0].classroom if len(enrolled) == 1 else None
            else:
                classroom = None
            if classroom is None or user is None or user.is_disabled:
                record_login_failure(request, login)
                return fail("Invalid credentials", 401)
            if user.two_factor_auth:
                tfa_code = data.get("tfaCode", data.get("tfa_code"))
                try:
                    valid_tfa = isinstance(tfa_code, str) and OtpAuth(user.tfa_token).valid_totp(tfa_code)
                except (TypeError, ValueError):
                    valid_tfa = False
                if not valid_tfa:
                    record_login_failure(request, login)
                    return fail("Two factor code required or invalid", 401)
            if not classroom_access(user, classroom, remote):
                return fail("Account unavailable on this network address", 403)
            auth.login(request, user)
            clear_login_failures(request, login)
            with transaction.atomic():
                locked = User.objects.select_for_update().get(id=user.id)
                keys = list(locked.session_keys or [])
                if request.session.session_key not in keys:
                    keys.append(request.session.session_key)
                    locked.session_keys = keys
                    locked.save(update_fields=["session_keys"])
            return respond({"username": user.username, "class": classroom.slug,
                            "teacher": is_classroom_teacher(user, classroom),
                            "role": class_json(classroom, user)["role"]})
        except ValueError:
            return fail("Invalid credentials", 401)


def assistant_json(assistant):
    return {"login": assistant.user.username, "name": assistant.user.userprofile.real_name,
            "createdAt": assistant.created_at.isoformat()}


class ClassroomAssistantsView(View):
    def get(self, request, slug):
        classroom = Classroom.objects.filter(slug=slug).first()
        if classroom is None or not is_classroom_owner(request.user, classroom):
            return fail("Class owner only", 403)
        assistants = ClassroomAssistant.objects.filter(classroom=classroom).select_related(
            "user", "user__userprofile").order_by("user__username")
        return respond([assistant_json(item) for item in assistants])

    def post(self, request, slug):
        classroom = Classroom.objects.filter(slug=slug).first()
        if classroom is None or not is_classroom_owner(request.user, classroom):
            return fail("Class owner only", 403)
        try:
            data = body_data(request)
            login = data.get("login")
            existing = data.get("existing", False)
            if not isinstance(login, str) or not LOGIN_RE.fullmatch(login.strip()) or \
                    not isinstance(existing, bool):
                raise ValueError("Invalid assistant login")
            login = login.strip()
            with transaction.atomic():
                Classroom.objects.select_for_update().get(id=classroom.id)
                user = User.objects.select_for_update().filter(username__iexact=login).first()
                if existing:
                    if user is None or user.is_disabled or user.is_admin_role() or \
                            Enrollment.objects.filter(user=user).exists() or \
                            not ClassroomAssistant.objects.filter(
                                user=user, classroom__teacher=classroom.teacher).exists():
                        raise ValueError("Assistant account is unavailable for this teacher")
                else:
                    name, password = data.get("name"), data.get("password")
                    if user is not None:
                        return fail("Login already exists", 409)
                    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or \
                            not isinstance(password, str) or not 12 <= len(password) <= 128 or \
                            password.casefold() == login.casefold():
                        raise ValueError("Name and a distinct password of 12 to 128 characters are required")
                    user = User(username=login, admin_type=AdminType.REGULAR_USER)
                    user.set_password(password)
                    user.save()
                    UserProfile.objects.create(user=user, real_name=name.strip())
                assistant = ClassroomAssistant.objects.create(classroom=classroom, user=user)
            return respond(assistant_json(assistant), status=201)
        except IntegrityError:
            return fail("Assistant already assigned", 409)
        except ValueError as exc:
            return fail(str(exc))


class ClassroomAssistantView(View):
    def delete(self, request, slug, login):
        classroom = Classroom.objects.filter(slug=slug).first()
        if classroom is None or not is_classroom_owner(request.user, classroom):
            return fail("Class owner only", 403)
        deleted, _ = ClassroomAssistant.objects.filter(
            classroom=classroom, user__username__iexact=login).delete()
        if not deleted:
            return fail("Assistant not found", 404)
        return respond({"removed": True})


class ClassroomRosterView(View):
    def get(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None or not is_classroom_teacher(request.user, classroom):
            return fail("Teacher only", 403)
        rows = Enrollment.objects.filter(classroom=classroom).select_related("user", "user__userprofile")
        return respond([{"login": item.user.username, "name": item.user.userprofile.real_name,
                         "class": item.class_name, "lockedIp": item.locked_ip,
                         "ipLockDisabled": item.ip_lock_disabled} for item in rows.order_by("user__username")])

    def post(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None or not is_classroom_teacher(request.user, classroom):
            return fail("Teacher only", 403)
        try:
            data = body_data(request)
            raw = data.get("csv")
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 262144:
                raise ValueError("CSV must be at most 256 KiB")
            reader = csv.DictReader(io.StringIO(raw.lstrip("\ufeff"), newline=""))
            if reader.fieldnames is None or {x.strip().lower() for x in reader.fieldnames} != {
                    "login", "name", "class", "password"}:
                raise ValueError("CSV headers must be login,name,class,password")
            reader.fieldnames = [x.strip().lower() for x in reader.fieldnames]
            rows = list(reader)
            if not rows or len(rows) > 500:
                raise ValueError("Import 1 to 500 students")
            seen = set()
            normalized = []
            for row in rows:
                if None in row or any(value is None for value in row.values()):
                    raise ValueError("Invalid CSV row")
                login = row["login"].strip()
                name = row["name"].strip()
                section = row["class"].strip()
                password = row["password"].strip()
                if not LOGIN_RE.fullmatch(login) or login.casefold() in seen or not 1 <= len(name) <= 80 or \
                        not 1 <= len(section) <= 80 or not 6 <= len(password) <= 128:
                    raise ValueError("Invalid or duplicate roster row")
                seen.add(login.casefold())
                normalized.append((login, name, section, password))
            with transaction.atomic():
                for login, name, section, password in normalized:
                    user = User.objects.select_for_update().filter(username__iexact=login).first()
                    if user is not None and (user.is_admin_role() or
                                             not Enrollment.objects.filter(classroom=classroom, user=user).exists()):
                        raise ValueError("An account belongs outside this class: " + login)
                    password_changed = user is None or not user.check_password(password)
                    if user is None:
                        user = User(username=login, admin_type=AdminType.REGULAR_USER)
                    if password_changed:
                        user.set_password(password)
                        user.save()
                    UserProfile.objects.update_or_create(user=user, defaults={"real_name": name})
                    enrollment, _ = Enrollment.objects.get_or_create(classroom=classroom, user=user)
                    enrollment.class_name = section
                    if password_changed:
                        enrollment.locked_ip = None
                        enrollment.save(update_fields=["class_name", "locked_ip"])
                        revoke_sessions(user)
                    else:
                        enrollment.save(update_fields=["class_name"])
            return respond({"imported": len(normalized)})
        except (ValueError, csv.Error) as exc:
            return fail(str(exc))
        except IntegrityError:
            return fail("Roster import conflicted with an existing account", 409)


class ClassroomIPLockView(View):
    def post(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None or not is_classroom_teacher(request.user, classroom):
            return fail("Teacher only", 403)
        try:
            data = body_data(request)
            action = data.get("action")
            with transaction.atomic():
                classroom = Classroom.objects.select_for_update().get(id=classroom.id)
                if action == "set-global" and isinstance(data.get("enabled"), bool):
                    classroom.ip_lock_enabled = data["enabled"]
                    classroom.save(update_fields=["ip_lock_enabled"])
                    if data["enabled"]:
                        for enrollment in Enrollment.objects.filter(classroom=classroom).select_related("user"):
                            enrollment.locked_ip = None
                            enrollment.save(update_fields=["locked_ip"])
                            revoke_sessions(enrollment.user)
                elif action == "reset-all":
                    for enrollment in Enrollment.objects.filter(classroom=classroom).select_related("user"):
                        enrollment.locked_ip = None
                        enrollment.save(update_fields=["locked_ip"])
                        revoke_sessions(enrollment.user)
                elif action in ("reset-student", "set-student"):
                    enrollment = Enrollment.objects.select_for_update().select_related("user").filter(
                        classroom=classroom, user__username__iexact=data.get("login")).first()
                    if enrollment is None:
                        raise ValueError("Student not found")
                    if action == "set-student":
                        if not isinstance(data.get("disabled"), bool):
                            raise ValueError("disabled must be true or false")
                        enrollment.ip_lock_disabled = data["disabled"]
                    enrollment.locked_ip = None
                    enrollment.save(update_fields=["ip_lock_disabled", "locked_ip"])
                    revoke_sessions(enrollment.user)
                else:
                    raise ValueError("Invalid IP lock action")
            return respond({"ipLockEnabled": classroom.ip_lock_enabled})
        except ValueError as exc:
            return fail(str(exc))


class ClassroomQuizzesView(View):
    def get(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None:
            return fail("Class unavailable", 404)
        teacher = is_classroom_teacher(request.user, classroom)
        quizzes = ClassroomQuiz.objects.filter(classroom=classroom).order_by("created_at", "id")
        if not teacher:
            quizzes = quizzes.filter(published=True, starts_at__lte=timezone.now())
        return respond([quiz_json(item, teacher) for item in quizzes])

    def post(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None or not is_classroom_teacher(request.user, classroom):
            return fail("Teacher only", 403)
        try:
            data = body_data(request)
            quiz_slug, title, mode = data.get("slug"), data.get("title"), data.get("mode")
            if not isinstance(quiz_slug, str) or not SLUG_RE.fullmatch(quiz_slug):
                raise ValueError("Invalid quiz ID")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160:
                raise ValueError("Invalid quiz title")
            if mode not in (ClassroomQuiz.PRACTICE, ClassroomQuiz.EXAM):
                raise ValueError("Invalid quiz mode")
            starts_at = parse_time(data.get("startsAt")) or timezone.now()
            ends_at = parse_time(data.get("endsAt"), required=(mode == ClassroomQuiz.EXAM))
            if ends_at and ends_at <= starts_at:
                raise ValueError("Quiz end must be after start")
            published = data.get("published", False)
            if not isinstance(published, bool):
                raise ValueError("published must be true or false")
            with transaction.atomic():
                contest = Contest.objects.create(title=title.strip(), description="", real_time_rank=False,
                                                 rule_type=ContestRuleType.OI, start_time=starts_at,
                                                 end_time=ends_at or PRACTICE_END,
                                                 created_by=classroom.teacher, visible=True,
                                                 allowed_ip_ranges=[])
                quiz = ClassroomQuiz.objects.create(classroom=classroom, contest=contest, slug=quiz_slug,
                                                    title=title.strip(), mode=mode, published=published,
                                                    starts_at=starts_at, ends_at=ends_at)
            return respond(quiz_json(quiz, True), status=201)
        except IntegrityError:
            return fail("Quiz ID already exists", 409)
        except ValueError as exc:
            return fail(str(exc))


class ClassroomQuizView(View):
    def get(self, request, slug, quiz_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        if quiz is None:
            return fail("Quiz unavailable", 404)
        teacher = is_classroom_teacher(request.user, classroom)
        if not teacher and not visible_to_student(quiz):
            return fail("Quiz unavailable", 404)
        data = quiz_json(quiz, teacher)
        data["questions"] = [question_json(q, teacher) for q in quiz.questions.all()
                             if teacher or q.published]
        return respond(data)

    def put(self, request, slug, quiz_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        if quiz is None or not is_classroom_teacher(request.user, classroom):
            return fail("Teacher only", 403)
        try:
            data = body_data(request)
            with transaction.atomic():
                quiz = ClassroomQuiz.objects.select_for_update().select_related("contest").get(id=quiz.id)
                if "title" in data:
                    if not isinstance(data["title"], str) or not 1 <= len(data["title"].strip()) <= 160:
                        raise ValueError("Invalid quiz title")
                    quiz.title = data["title"].strip()
                    quiz.contest.title = quiz.title
                if "published" in data:
                    if not isinstance(data["published"], bool):
                        raise ValueError("published must be true or false")
                    quiz.published = data["published"]
                if "endsAt" in data:
                    ends_at = parse_time(data["endsAt"], required=(quiz.mode == ClassroomQuiz.EXAM))
                    if ends_at and ends_at <= quiz.starts_at:
                        raise ValueError("Quiz end must be after start")
                    quiz.ends_at = ends_at
                    quiz.contest.end_time = ends_at or PRACTICE_END
                quiz.save()
                quiz.contest.save()
            return respond(quiz_json(quiz, True))
        except ValueError as exc:
            return fail(str(exc))


def validated_question(data, quiz, old=None):
    slug, title, kind = data.get("slug"), data.get("title"), data.get("kind")
    if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug):
        raise ValueError("Invalid question ID")
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 160:
        raise ValueError("Invalid question title")
    if kind not in (ClassroomQuestion.MCQ, ClassroomQuestion.C_LAB):
        raise ValueError("Invalid question kind")
    points = data.get("points")
    if type(points) is not int or not 1 <= points <= 1000:
        raise ValueError("Points must be 1 to 1000")
    prompt = data.get("prompt", "")
    if not isinstance(prompt, str) or len(prompt) > 20000:
        raise ValueError("Invalid prompt")
    position = data.get("position", 0)
    if type(position) is not int or not 0 <= position <= 10000:
        raise ValueError("Invalid position")
    published = data.get("published", True)
    if not isinstance(published, bool):
        raise ValueError("Invalid publication status")
    attrs = {"quiz": quiz, "slug": slug, "title": title.strip(), "kind": kind,
             "points": points, "prompt": prompt, "position": position, "published": published}
    if kind == ClassroomQuestion.MCQ:
        choices, answer = data.get("choices"), data.get("answerIndex")
        if not isinstance(choices, list) or not 2 <= len(choices) <= 8 or \
                not all(isinstance(x, str) and 1 <= len(x.strip()) <= 500 for x in choices) or \
                type(answer) is not int or not 0 <= answer < len(choices):
            raise ValueError("Invalid choices or answer")
        attrs.update({"choices": [x.strip() for x in choices], "answer_index": answer,
                      "problem": None})
    else:
        problem_id = data.get("problemId")
        problem = Problem.objects.filter(id=problem_id, contest=quiz.contest, visible=True,
                                         rule_type=ProblemRuleType.OI).first()
        if problem is None or problem.languages != ["C"] or problem.total_score <= 0 or \
                problem.io_mode.get("io_mode") != "Standard IO":
            raise ValueError("Link a scored OI C-only standard-input problem in this quiz")
        if ClassroomQuestion.objects.filter(problem=problem).exclude(id=old.id if old else None).exists():
            raise ValueError("C problem is already linked")
        attrs.update({"problem": problem, "choices": [], "answer_index": None})
    return attrs


class ClassroomQuestionsView(View):
    def get(self, request, slug, quiz_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        if quiz is None:
            return fail("Quiz unavailable", 404)
        teacher = is_classroom_teacher(request.user, classroom)
        if not teacher and not visible_to_student(quiz):
            return fail("Quiz unavailable", 404)
        return respond([question_json(q, teacher) for q in quiz.questions.all()
                        if teacher or q.published])

    def post(self, request, slug, quiz_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        if quiz is None or not is_classroom_teacher(request.user, classroom):
            return fail("Teacher only", 403)
        try:
            attrs = validated_question(body_data(request), quiz)
            question = ClassroomQuestion.objects.create(**attrs)
            return respond(question_json(question, True), status=201)
        except IntegrityError:
            return fail("Question ID already exists", 409)
        except ValueError as exc:
            return fail(str(exc))


class ClassroomQuestionView(View):
    def get(self, request, slug, quiz_id, question_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        question = ClassroomQuestion.objects.filter(quiz=quiz, id=question_id).first() if quiz else None
        if question is None:
            return fail("Question unavailable", 404)
        teacher = is_classroom_teacher(request.user, classroom)
        if not teacher and (not visible_to_student(quiz) or not question.published):
            return fail("Question unavailable", 404)
        return respond(question_json(question, teacher))

    def put(self, request, slug, quiz_id, question_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        question = ClassroomQuestion.objects.filter(quiz=quiz, id=question_id).first() if quiz else None
        if question is None or not is_classroom_teacher(request.user, classroom):
            return fail("Teacher only", 403)
        try:
            data = body_data(request)
            with transaction.atomic():
                question = ClassroomQuestion.objects.select_for_update().get(id=question.id)
                attrs = validated_question(data, quiz, question)
                submitted = question.attempts.exists()
                if submitted:
                    changed = any(getattr(question, key) != value for key, value in attrs.items()
                                  if key not in ("points", "position", "published", "quiz"))
                    if changed:
                        return fail("Question content is frozen after the first submission", 409)
                old_points = question.points
                for key, value in attrs.items():
                    setattr(question, key, value)
                question.save()
                if submitted and old_points != question.points:
                    # PostgreSQL cannot lock the nullable submission side of an outer join.
                    # Lock attempt rows first, then fetch their related objects separately.
                    attempts = list(question.attempts.select_for_update())
                    for attempt in attempts:
                        if question.kind == ClassroomQuestion.MCQ:
                            attempt.earned = question.points if attempt.choice_index == question.answer_index else 0
                        elif attempt.submission_id and attempt.verdict not in (
                                str(JudgeStatus.PENDING), str(JudgeStatus.JUDGING), str(JudgeStatus.SYSTEM_ERROR)):
                            attempt.earned = c_earned(question, attempt.submission)
                        else:
                            attempt.earned = 0
                        attempt.save(update_fields=["earned"])
                    for user in {attempt.user for attempt in attempts}:
                        refresh_score(question, user)
            return respond(question_json(question, True))
        except IntegrityError:
            return fail("Question ID already exists", 409)
        except ValueError as exc:
            return fail(str(exc))


class ClassroomDraftView(View):
    def get(self, request, slug, quiz_id, question_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        question = ClassroomQuestion.objects.filter(quiz=quiz, id=question_id,
                                                    kind=ClassroomQuestion.C_LAB).first() if quiz else None
        if question is None or is_classroom_teacher(request.user, classroom):
            return fail("Draft unavailable", 404)
        draft = ClassroomDraft.objects.filter(question=question, user=request.user).first()
        return respond({"code": draft.code if draft else "", "updatedAt": draft.updated_at.isoformat() if draft else None})

    def post(self, request, slug, quiz_id, question_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        question = ClassroomQuestion.objects.filter(quiz=quiz, id=question_id,
                                                    kind=ClassroomQuestion.C_LAB, published=True).first() if quiz else None
        if question is None or is_classroom_teacher(request.user, classroom) or not open_for_student(quiz):
            return fail("Question unavailable", 403)
        try:
            data = body_data(request)
            code = data.get("code")
            if not isinstance(code, str) or len(code.encode("utf-8")) > 16000:
                raise ValueError("C source must be at most 16 KB")
            with transaction.atomic():
                quiz = ClassroomQuiz.objects.select_for_update().get(id=quiz.id)
                if not open_for_student(quiz):
                    return fail("Quiz is closed", 403)
                draft, _ = ClassroomDraft.objects.update_or_create(question=question, user=request.user,
                                                                   defaults={"code": code})
            return respond({"updatedAt": draft.updated_at.isoformat()})
        except ValueError as exc:
            return fail(str(exc))


class ClassroomCCheckView(View):
    """A bounded compile-only request to the private JudgeServer."""

    def post(self, request, slug, quiz_id, question_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        question = ClassroomQuestion.objects.filter(quiz=quiz, id=question_id,
                                                    kind=ClassroomQuestion.C_LAB).first() if quiz else None
        teacher = is_classroom_teacher(request.user, classroom) if classroom else False
        if question is None or (not teacher and (not question.published or not open_for_student(quiz))):
            return fail("Question unavailable", 403)
        try:
            data = body_data(request)
            code = data.get("code")
            if not isinstance(code, str) or len(code.encode("utf-8")) > 16000:
                raise ValueError("C source must be at most 16 KB")
            if not code.strip():
                return respond({"ok": True, "diagnostics": []})
            syntax_server_url = os.environ.get("FCU_SYNTAX_SERVER_URL")
            if not syntax_server_url:
                return fail("C compiler temporarily unavailable", 503)
            key = "fcu-c-check:{}".format(request.user.id)
            if not cache.add(key, 1, timeout=2):
                return fail("Syntax check is cooling down", 429)
            token = hashlib.sha256(SysOptions.judge_server_token.encode("utf-8")).hexdigest()
            response = requests.post(urljoin(syntax_server_url, "/check_c_syntax"), json={"src": code},
                                     headers={"X-Judge-Server-Token": token}, timeout=15)
            response.raise_for_status()
            try:
                result = response.json()
            except ValueError:
                return fail("Invalid compiler response", 503)
            if not isinstance(result, dict):
                return fail("Invalid compiler response", 503)
            if result.get("err"):
                return fail("C compiler temporarily unavailable", 503)
            result_data = result.get("data")
            if not isinstance(result_data, dict) or not isinstance(result_data.get("ok"), bool) or \
                    not isinstance(result_data.get("diagnostics"), list):
                return fail("Invalid compiler response", 503)
            diagnostics = []
            for item in result_data["diagnostics"][:30]:
                if not isinstance(item, dict):
                    continue
                severity = item.get("severity")
                if severity not in ("error", "warning", "note"):
                    continue
                diagnostics.append({"line": item.get("line") if type(item.get("line")) is int else None,
                                    "column": item.get("column") if type(item.get("column")) is int else None,
                                    "severity": severity, "message": str(item.get("message", ""))[:500]})
            return respond({"ok": result_data["ok"], "diagnostics": diagnostics})
        except ValueError as exc:
            return fail(str(exc))
        except (requests.RequestException, KeyError, TypeError):
            return fail("C compiler temporarily unavailable", 503)


class ClassroomCRunView(View):
    """Run C against only the caller's input in a separate sandbox container."""

    def post(self, request, slug, quiz_id, question_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        question = ClassroomQuestion.objects.filter(quiz=quiz, id=question_id,
                                                    kind=ClassroomQuestion.C_LAB).first() if quiz else None
        teacher = is_classroom_teacher(request.user, classroom) if classroom else False
        if question is None or (not teacher and (not question.published or not open_for_student(quiz))):
            return fail("Question unavailable", 403)
        try:
            data = body_data(request)
            code, stdin = data.get("code"), data.get("input")
            if not isinstance(code, str) or not isinstance(stdin, str):
                raise ValueError("C source and input must be text")
            if len(code.encode("utf-8")) > 16_000 or len(stdin.encode("utf-8")) > 32_000:
                raise ValueError("C source or input exceeds the size limit")
            if not code.strip():
                raise ValueError("C source is required")
            syntax_server_url = os.environ.get("FCU_SYNTAX_SERVER_URL")
            if not syntax_server_url:
                return fail("C runner temporarily unavailable", 503)
            key = "fcu-c-run:{}".format(request.user.id)
            if not cache.add(key, 1, timeout=30):
                return fail("A C run is already in progress", 429)
            try:
                token = hashlib.sha256(SysOptions.judge_server_token.encode("utf-8")).hexdigest()
                response = requests.post(urljoin(syntax_server_url, "/run_c"),
                                         json={"src": code, "input": stdin},
                                         headers={"X-Judge-Server-Token": token}, timeout=20)
                response.raise_for_status()
                try:
                    result = response.json()
                except ValueError:
                    return fail("Invalid C runner response", 503)
            finally:
                cache.delete(key)
            if not isinstance(result, dict) or result.get("err"):
                return fail("C runner temporarily unavailable", 503)
            output = result.get("data")
            if (not isinstance(output, dict) or
                    output.get("status") not in ("ok", "compile_error", "runtime_error",
                                                 "time_limit", "memory_limit", "output_limit") or
                    not isinstance(output.get("stdout"), str) or
                    not isinstance(output.get("stderr"), str) or
                    (output.get("exitCode") is not None and type(output.get("exitCode")) is not int) or
                    type(output.get("timeMs")) is not int):
                return fail("Invalid C runner response", 503)
            return respond({"status": output["status"], "stdout": output["stdout"][:65536],
                            "stderr": output["stderr"][:65536],
                            "exitCode": output["exitCode"], "timeMs": max(0, output["timeMs"])})
        except ValueError as exc:
            return fail(str(exc))
        except (requests.RequestException, KeyError, TypeError):
            return fail("C runner temporarily unavailable", 503)


class ClassroomMCQSubmitView(View):
    def post(self, request, slug, quiz_id, question_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        question = ClassroomQuestion.objects.filter(quiz=quiz, id=question_id,
                                                    kind=ClassroomQuestion.MCQ, published=True).first() if quiz else None
        if question is None or is_classroom_teacher(request.user, classroom):
            return fail("Question unavailable", 404)
        try:
            data = body_data(request)
            choice = data.get("choiceIndex")
            if type(choice) is not int or not 0 <= choice < len(question.choices):
                raise ValueError("Choose an answer")
            with transaction.atomic():
                quiz = ClassroomQuiz.objects.select_for_update().get(id=quiz.id)
                User.objects.select_for_update().get(id=request.user.id)
                question = ClassroomQuestion.objects.select_for_update().get(id=question.id)
                if not open_for_student(quiz) or not question.published:
                    return fail("Quiz is closed", 403)
                if choice >= len(question.choices):
                    return fail("Choose an answer")
                if quiz.mode == ClassroomQuiz.EXAM and ClassroomAttempt.objects.filter(
                        question=question, user=request.user).exists():
                    return fail("This exam choice question was already submitted", 409)
                earned = question.points if choice == question.answer_index else 0
                attempt = ClassroomAttempt.objects.create(question=question, user=request.user,
                                                          choice_index=choice, earned=earned,
                                                          verdict="Accepted" if earned else "Wrong Answer")
                refresh_score(question, request.user)
            return respond({"attemptId": attempt.id, "earned": earned, "max": question.points,
                            "correct": earned == question.points}, status=201)
        except ValueError as exc:
            return fail(str(exc))


class ClassroomStateView(View):
    def get(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None:
            return fail("Class unavailable", 404)
        teacher = is_classroom_teacher(request.user, classroom)
        quizzes = list(ClassroomQuiz.objects.filter(classroom=classroom).prefetch_related("questions"))
        if not teacher:
            quizzes = [item for item in quizzes if visible_to_student(item)]
        scores = {} if teacher else {
            str(x.question_id): {"best": x.best_earned, "latest": x.latest_earned, "attempts": x.attempts}
            for x in ClassroomScore.objects.filter(user=request.user, question__quiz__classroom=classroom)
        }
        drafts = {} if teacher else {str(x.question_id): x.code for x in ClassroomDraft.objects.filter(
            user=request.user, question__quiz__classroom=classroom)}
        return respond({"actor": {"username": request.user.username, "teacher": teacher},
                        "classroom": class_json(classroom, request.user), "serverNow": timezone.now().isoformat(),
                        "quizzes": [{**quiz_json(item, teacher), "questions": [question_json(q, teacher)
                                     for q in item.questions.all() if teacher or q.published]} for item in quizzes],
                        "scores": scores, "drafts": drafts})


class ClassroomScoresView(View):
    def get(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None or not is_classroom_teacher(request.user, classroom):
            return fail("Teacher only", 403)
        scores = ClassroomScore.objects.filter(question__quiz__classroom=classroom)
        by_user = {}
        for score in scores:
            by_user.setdefault(score.user_id, {})[str(score.question_id)] = {
                "best": score.best_earned, "latest": score.latest_earned, "attempts": score.attempts}
        rows = Enrollment.objects.filter(classroom=classroom).select_related("user", "user__userprofile")
        return respond([{"login": row.user.username, "name": row.user.userprofile.real_name,
                         "class": row.class_name, "scores": by_user.get(row.user_id, {})}
                        for row in rows.order_by("user__username")])


class ClassroomSubmissionsView(View):
    def get(self, request, slug, quiz_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        if quiz is None:
            return fail("Quiz unavailable", 404)
        teacher = is_classroom_teacher(request.user, classroom)
        username = request.GET.get("login")
        if username and not teacher:
            return fail("Teacher only", 403)
        query = ClassroomAttempt.objects.filter(question__quiz=quiz).select_related(
            "question", "user", "submission").annotate(
                submitted_at=Coalesce("submission__create_time", "created_at")).order_by("-submitted_at", "-id")
        if username:
            query = query.filter(user__username__iexact=username)
        elif not teacher:
            query = query.filter(user=request.user)
        rows = []
        for attempt in query[:500]:
            verdict = str(attempt.submission.result) if attempt.submission_id else attempt.verdict
            row = {"id": attempt.id, "questionId": attempt.question_id,
                   "earned": attempt.earned, "max": attempt.question.points,
                   "verdict": verdict, "at": attempt.submitted_at.isoformat(),
                   "choiceIndex": attempt.choice_index, "submissionId": attempt.submission_id}
            if teacher:
                row["login"] = attempt.user.username
            if attempt.submission_id:
                row["code"] = attempt.submission.code
            rows.append(row)
        return respond(rows)


class ClassroomPracticeTestsView(View):
    """Reveal every graded case only for an owned practice submission."""

    @staticmethod
    def read_case_file(directory, filename):
        if not isinstance(filename, str) or not re.fullmatch(r"[0-9]+\.(?:in|out)", filename):
            raise ValueError("Invalid test file")
        path = os.path.realpath(os.path.join(directory, filename))
        if not path.startswith(directory + os.sep):
            raise ValueError("Invalid test file")
        with open(path, "rb") as source:
            content = source.read(16385)
        return content[:16384].decode("utf-8", "replace"), len(content) > 16384

    def get(self, request, slug, quiz_id, submission_id):
        classroom, quiz = get_quiz(request, slug, quiz_id)
        if quiz is None:
            return fail("Quiz unavailable", 404)
        if quiz.mode != ClassroomQuiz.PRACTICE:
            return fail("Exam test cases remain private", 403)
        teacher = is_classroom_teacher(request.user, classroom)
        query = ClassroomAttempt.objects.select_related("question__problem", "submission").filter(
            question__quiz=quiz, question__kind=ClassroomQuestion.C_LAB,
            submission_id=submission_id)
        if not teacher:
            query = query.filter(user=request.user, question__published=True)
        attempt = query.first()
        if attempt is None or (not teacher and not visible_to_student(quiz)):
            return fail("Submission unavailable", 404)
        submission = attempt.submission
        if submission.result in (JudgeStatus.PENDING, JudgeStatus.JUDGING):
            return fail("Judging is still in progress", 409)
        problem = attempt.question.problem
        if problem is None or problem.spj or not isinstance(problem.test_case_score, list):
            return fail("Test data unavailable", 503)
        case_id = problem.test_case_id
        if not isinstance(case_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", case_id):
            return fail("Test data unavailable", 503)
        directory = os.path.realpath(os.path.join(settings.TEST_CASE_DIR, case_id))
        root = os.path.realpath(settings.TEST_CASE_DIR)
        if not directory.startswith(root + os.sep):
            return fail("Test data unavailable", 503)
        judged = submission.info.get("data", []) if isinstance(submission.info, dict) else []
        judged_by_number = {str(item.get("test_case")): item for item in judged if isinstance(item, dict)} \
            if isinstance(judged, list) else {}
        cases = []
        try:
            for index, meta in enumerate(problem.test_case_score, start=1):
                if not isinstance(meta, dict):
                    raise ValueError("Invalid test metadata")
                stdin, input_truncated = self.read_case_file(directory, meta.get("input_name"))
                expected, expected_truncated = self.read_case_file(directory, meta.get("output_name"))
                outcome = judged_by_number.get(str(index), {})
                cases.append({"number": index, "input": stdin, "expected": expected,
                              "inputTruncated": input_truncated, "expectedTruncated": expected_truncated,
                              "verdict": outcome.get("result") if type(outcome.get("result")) is int else None,
                              "earnedWeight": outcome.get("score") if type(outcome.get("score")) is int else 0,
                              "maxWeight": meta.get("score") if type(meta.get("score")) is int else 0})
        except (OSError, ValueError):
            return fail("Test data unavailable", 503)
        return respond({"submissionId": submission.id, "questionId": attempt.question_id,
                        "earned": attempt.earned, "max": attempt.question.points,
                        "cases": cases})
