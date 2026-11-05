from django.conf import settings
from django.db import models

from contest.models import Contest
from problem.models import Problem
from submission.models import Submission


class Classroom(models.Model):
    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=160)
    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="fcu_classes_taught")
    ip_lock_enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fcu_classroom"


class ClassroomAssistant(models.Model):
    classroom = models.ForeignKey(Classroom, on_delete=models.CASCADE, related_name="assistants")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="fcu_classes_assisted")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fcu_classroom_assistant"
        unique_together = (("classroom", "user"),)


class Enrollment(models.Model):
    classroom = models.ForeignKey(Classroom, on_delete=models.CASCADE, related_name="enrollments")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fcu_enrollments")
    class_name = models.CharField(max_length=80, blank=True)
    locked_ip = models.GenericIPAddressField(null=True, blank=True)
    ip_lock_disabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fcu_enrollment"
        unique_together = (("classroom", "user"),)


class ClassroomQuiz(models.Model):
    PRACTICE = "practice"
    EXAM = "exam"
    MODES = ((PRACTICE, "Practice"), (EXAM, "Exam"))

    classroom = models.ForeignKey(Classroom, on_delete=models.CASCADE, related_name="quizzes")
    contest = models.OneToOneField(Contest, on_delete=models.PROTECT, related_name="fcu_quiz")
    slug = models.SlugField(max_length=64)
    title = models.CharField(max_length=160)
    mode = models.CharField(max_length=8, choices=MODES)
    published = models.BooleanField(default=False)
    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fcu_quiz"
        unique_together = (("classroom", "slug"),)


class ClassroomQuestion(models.Model):
    MCQ = "mcq"
    C_LAB = "c_lab"
    KINDS = ((MCQ, "Multiple choice"), (C_LAB, "C lab"))

    quiz = models.ForeignKey(ClassroomQuiz, on_delete=models.CASCADE, related_name="questions")
    problem = models.OneToOneField(Problem, null=True, blank=True, on_delete=models.PROTECT,
                                   related_name="fcu_question")
    slug = models.SlugField(max_length=64)
    title = models.CharField(max_length=160)
    prompt = models.TextField(blank=True)
    kind = models.CharField(max_length=5, choices=KINDS)
    points = models.PositiveIntegerField()
    position = models.PositiveIntegerField(default=0)
    published = models.BooleanField(default=True)
    choices = models.JSONField(default=list)
    answer_index = models.PositiveSmallIntegerField(null=True, blank=True)

    class Meta:
        db_table = "fcu_question"
        unique_together = (("quiz", "slug"),)
        ordering = ("position", "id")


class ClassroomDraft(models.Model):
    question = models.ForeignKey(ClassroomQuestion, on_delete=models.CASCADE, related_name="drafts")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    code = models.TextField()
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fcu_draft"
        unique_together = (("question", "user"),)


class ClassroomAttempt(models.Model):
    question = models.ForeignKey(ClassroomQuestion, on_delete=models.CASCADE, related_name="attempts")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fcu_attempts")
    submission = models.OneToOneField(Submission, null=True, blank=True, on_delete=models.CASCADE,
                                      related_name="fcu_attempt")
    choice_index = models.PositiveSmallIntegerField(null=True, blank=True)
    earned = models.PositiveIntegerField(default=0)
    verdict = models.CharField(max_length=40)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "fcu_attempt"
        ordering = ("-created_at", "-id")


class ClassroomScore(models.Model):
    question = models.ForeignKey(ClassroomQuestion, on_delete=models.CASCADE, related_name="scores")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="fcu_scores")
    best_earned = models.PositiveIntegerField(default=0)
    latest_earned = models.PositiveIntegerField(default=0)
    attempts = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "fcu_score"
        unique_together = (("question", "user"),)
