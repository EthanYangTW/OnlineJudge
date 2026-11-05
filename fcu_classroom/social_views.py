"""Classroom-scoped practice activity visible to enrolled classmates.

These views deliberately expose only aggregate scores and public submission
outcomes. Exam attempts and source code remain on the owner/teacher APIs.
"""

from django.utils import timezone
from django.views import View

from .access import is_classroom_teacher
from .models import ClassroomAttempt, ClassroomQuestion, ClassroomQuiz, ClassroomScore, Enrollment
from .views import fail, get_class, get_quiz, respond, visible_to_student


def _practice_quiz(request, slug, quiz_id):
    classroom, quiz = get_quiz(request, slug, quiz_id)
    if quiz is None:
        return None
    if quiz.mode != ClassroomQuiz.PRACTICE:
        return None
    if not is_classroom_teacher(request.user, classroom) and not visible_to_student(quiz):
        return None
    return quiz


def _pagination(request):
    try:
        page = int(request.GET.get("page", "1"))
        page_size = int(request.GET.get("pageSize", "20"))
    except (TypeError, ValueError):
        return None
    if not 1 <= page <= 1000 or not 1 <= page_size <= 100:
        return None
    return page, page_size


def _display_name(user):
    profile = getattr(user, "userprofile", None)
    return getattr(profile, "real_name", "") or user.username


def _visible_practice_quizzes(request, classroom):
    quizzes = ClassroomQuiz.objects.filter(classroom=classroom, mode=ClassroomQuiz.PRACTICE)
    if not is_classroom_teacher(request.user, classroom):
        quizzes = quizzes.filter(published=True, starts_at__lte=timezone.now())
    return quizzes


def _leaderboard_response(classroom, questions, paging):
    page, page_size = paging
    points = {question.id: question.points for question in questions}
    max_score = sum(points.values())
    by_user = {}
    for score in ClassroomScore.objects.filter(question_id__in=points):
        entry = by_user.setdefault(score.user_id, {"score": 0, "solved": 0, "attempts": 0})
        entry["score"] += score.best_earned
        entry["solved"] += int(points[score.question_id] == score.best_earned and score.best_earned > 0)
        entry["attempts"] += score.attempts

    rows = []
    enrollments = Enrollment.objects.filter(classroom=classroom).select_related("user", "user__userprofile")
    for enrollment in enrollments:
        user = enrollment.user
        if user.is_disabled:
            continue
        summary = by_user.get(user.id, {"score": 0, "solved": 0, "attempts": 0})
        rows.append({"login": user.username, "name": _display_name(user),
                     "score": summary["score"], "maxScore": max_score,
                     "solved": summary["solved"], "attempts": summary["attempts"]})
    rows.sort(key=lambda row: (-row["score"], -row["solved"], row["login"].casefold()))
    total = len(rows)
    start = (page - 1) * page_size
    page_rows = [{"rank": start + offset + 1, **row}
                 for offset, row in enumerate(rows[start:start + page_size])]
    return respond({"entries": page_rows, "page": page, "pageSize": page_size, "total": total})


def _status_response(question_ids, paging):
    page, page_size = paging
    query = ClassroomAttempt.objects.filter(question_id__in=question_ids, user__is_disabled=False).select_related(
        "question", "question__quiz", "user", "user__userprofile", "submission").order_by("-created_at", "-id")
    total = query.count()
    start = (page - 1) * page_size
    entries = []
    for attempt in query[start:start + page_size]:
        verdict = str(attempt.submission.result) if attempt.submission_id else attempt.verdict
        entries.append({"login": attempt.user.username, "name": _display_name(attempt.user),
                        "quizId": attempt.question.quiz_id, "quizTitle": attempt.question.quiz.title,
                        "questionId": attempt.question_id, "questionTitle": attempt.question.title,
                        "kind": attempt.question.kind, "verdict": verdict,
                        "earned": attempt.earned, "maxScore": attempt.question.points,
                        "at": timezone.localtime(attempt.created_at).isoformat()})
    return respond({"entries": entries, "page": page, "pageSize": page_size, "total": total})


class PracticeLeaderboardView(View):
    def get(self, request, slug, quiz_id):
        quiz = _practice_quiz(request, slug, quiz_id)
        if quiz is None:
            return fail("Practice quiz unavailable", 403)
        paging = _pagination(request)
        if paging is None:
            return fail("Invalid page or pageSize")
        questions = list(ClassroomQuestion.objects.filter(quiz=quiz, published=True).only("id", "points"))
        return _leaderboard_response(quiz.classroom, questions, paging)


class PracticeStatusView(View):
    def get(self, request, slug, quiz_id):
        quiz = _practice_quiz(request, slug, quiz_id)
        if quiz is None:
            return fail("Practice quiz unavailable", 403)
        paging = _pagination(request)
        if paging is None:
            return fail("Invalid page or pageSize")
        question_ids = ClassroomQuestion.objects.filter(quiz=quiz, published=True).values_list("id", flat=True)
        return _status_response(question_ids, paging)


class ClassPracticeLeaderboardView(View):
    def get(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None:
            return fail("Class unavailable", 403)
        paging = _pagination(request)
        if paging is None:
            return fail("Invalid page or pageSize")
        quizzes = _visible_practice_quizzes(request, classroom)
        if not quizzes.exists():
            page, page_size = paging
            return respond({"entries": [], "page": page, "pageSize": page_size, "total": 0})
        questions = list(ClassroomQuestion.objects.filter(quiz__in=quizzes, published=True).only("id", "points"))
        return _leaderboard_response(classroom, questions, paging)


class ClassPracticeStatusView(View):
    def get(self, request, slug):
        classroom = get_class(request, slug)
        if classroom is None:
            return fail("Class unavailable", 403)
        paging = _pagination(request)
        if paging is None:
            return fail("Invalid page or pageSize")
        quizzes = _visible_practice_quizzes(request, classroom)
        question_ids = ClassroomQuestion.objects.filter(quiz__in=quizzes, published=True).values_list("id", flat=True)
        return _status_response(question_ids, paging)
