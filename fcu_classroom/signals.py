from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from submission.models import JudgeStatus, Submission

from .models import ClassroomAttempt, ClassroomQuestion, ClassroomScore, Enrollment


def c_earned(question, submission):
    raw = submission.statistic_info.get("score", 0) if submission.statistic_info else 0
    denominator = max(question.problem.total_score, 1)
    return max(0, min(question.points, int(question.points * raw / denominator)))


def refresh_score(question, user):
    order_field = "submission__create_time" if question.kind == ClassroomQuestion.C_LAB else "created_at"
    attempts = ClassroomAttempt.objects.filter(question=question, user=user).order_by(order_field, "id")
    if question.kind == ClassroomQuestion.C_LAB:
        unfinished = [str(status) for status in (JudgeStatus.PENDING, JudgeStatus.JUDGING,
                                                 JudgeStatus.SYSTEM_ERROR)]
        attempts = attempts.exclude(verdict__in=unfinished)
    values = list(attempts.values_list("earned", flat=True))
    if not values:
        ClassroomScore.objects.filter(question=question, user=user).delete()
        return
    ClassroomScore.objects.update_or_create(question=question, user=user,
                                            defaults={"best_earned": max(values),
                                                      "latest_earned": values[-1], "attempts": len(values)})


@receiver(post_save, sender=Submission)
def save_c_result(sender, instance, **kwargs):
    question = ClassroomQuestion.objects.select_related("quiz", "problem").filter(problem_id=instance.problem_id).first()
    if question is None or instance.contest_id != question.quiz.contest_id:
        return
    if instance.create_time and (instance.create_time < question.quiz.starts_at or
                                 (question.quiz.ends_at and instance.create_time > question.quiz.ends_at)):
        return
    from account.models import User
    try:
        user = User.objects.get(id=instance.user_id)
    except User.DoesNotExist:
        return
    if not Enrollment.objects.filter(classroom=question.quiz.classroom, user=user).exists():
        return
    with transaction.atomic():
        question = ClassroomQuestion.objects.select_for_update().get(id=question.id)
        final_result = instance.result not in (JudgeStatus.PENDING, JudgeStatus.JUDGING,
                                               JudgeStatus.SYSTEM_ERROR)
        if not final_result:
            previous = ClassroomAttempt.objects.filter(submission=instance).first()
            if previous and previous.verdict not in (str(JudgeStatus.PENDING), str(JudgeStatus.JUDGING),
                                                     str(JudgeStatus.SYSTEM_ERROR)):
                return
        earned = c_earned(question, instance) if final_result else 0
        ClassroomAttempt.objects.update_or_create(submission=instance,
                                                  defaults={"question": question, "user": user,
                                                            "earned": earned, "verdict": str(instance.result)})
        if final_result:
            refresh_score(question, user)
