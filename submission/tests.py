from copy import deepcopy
import os
from unittest import mock
from datetime import timedelta

from django.utils import timezone
from contest.models import Contest
from fcu_classroom.models import Classroom, ClassroomQuiz, ClassroomQuestion, Enrollment
from problem.models import Problem, ProblemTag
from utils.api.tests import APITestCase
from .models import Submission

DEFAULT_PROBLEM_DATA = {"_id": "A-110", "title": "test", "description": "<p>test</p>", "input_description": "test",
                        "output_description": "test", "time_limit": 1000, "memory_limit": 256, "difficulty": "Low",
                        "visible": True, "tags": ["test"], "languages": ["C", "C++", "Java", "Python2"], "template": {},
                        "samples": [{"input": "test", "output": "test"}], "spj": False, "spj_language": "C",
                        "spj_code": "", "test_case_id": "499b26290cc7994e0b497212e842ea85",
                        "test_case_score": [{"output_name": "1.out", "input_name": "1.in", "output_size": 0,
                                             "stripped_output_md5": "d41d8cd98f00b204e9800998ecf8427e",
                                             "input_size": 0, "score": 0}],
                        "rule_type": "ACM", "hint": "<p>test</p>", "source": "test"}

DEFAULT_SUBMISSION_DATA = {
    "problem_id": "1",
    "user_id": 1,
    "username": "test",
    "code": "xxxxxxxxxxxxxx",
    "result": -2,
    "info": {},
    "language": "C",
    "statistic_info": {}
}


# todo contest submission


class SubmissionPrepare(APITestCase):
    def _create_problem_and_submission(self):
        user = self.create_admin("test", "test123", login=False)
        problem_data = deepcopy(DEFAULT_PROBLEM_DATA)
        tags = problem_data.pop("tags")
        problem_data["created_by"] = user
        self.problem = Problem.objects.create(**problem_data)
        for tag in tags:
            tag = ProblemTag.objects.create(name=tag)
            self.problem.tags.add(tag)
        self.problem.save()
        self.submission_data = deepcopy(DEFAULT_SUBMISSION_DATA)
        self.submission_data["problem_id"] = self.problem.id
        self.submission = Submission.objects.create(**self.submission_data)


class SubmissionListTest(SubmissionPrepare):
    def setUp(self):
        self._create_problem_and_submission()
        self.create_user("123", "345")
        self.url = self.reverse("submission_list_api")

    def test_get_submission_list(self):
        resp = self.client.get(self.url, data={"limit": "10"})
        self.assertSuccess(resp)


@mock.patch("submission.views.oj.judge_task.send")
class SubmissionAPITest(SubmissionPrepare):
    def setUp(self):
        self._create_problem_and_submission()
        self.user = self.create_user("123", "test123")
        self.url = self.reverse("submission_api")

    def test_create_submission(self, judge_task):
        resp = self.client.post(self.url, self.submission_data)
        self.assertSuccess(resp)
        judge_task.assert_called()

    def test_create_submission_with_wrong_language(self, judge_task):
        self.submission_data.update({"language": "Python3"})
        resp = self.client.post(self.url, self.submission_data)
        self.assertFailed(resp)
        self.assertDictEqual(resp.data, {"error": "invalid-language",
                                         "data": "language: Python3 is not a valid language"})
        judge_task.assert_not_called()


class ClassroomSubmissionPrivacyTests(APITestCase):
    def setUp(self):
        self.teacher = self.create_admin("teacher", "teacher-pass", login=False)
        self.first_student = self.create_user("student-one", "student-pass", login=False)
        self.second_student = self.create_user("student-two", "student-pass", login=False)
        self.outsider = self.create_user("outsider", "student-pass", login=False)
        self.classroom = Classroom.objects.create(slug="c-privacy", name="C privacy", teacher=self.teacher)
        for student in (self.first_student, self.second_student):
            Enrollment.objects.create(classroom=self.classroom, user=student, ip_lock_disabled=True)
        start = timezone.now() - timedelta(minutes=1)
        end = timezone.now() + timedelta(hours=1)
        self.contest = Contest.objects.create(
            title="C exam", description="", real_time_rank=False, password=None,
            rule_type="OI", start_time=start, end_time=end,
            created_by=self.teacher, visible=True, allowed_ip_ranges=[])
        self.quiz = ClassroomQuiz.objects.create(classroom=self.classroom, contest=self.contest,
                                                 slug="exam", title="C exam", mode="exam", published=True,
                                                 starts_at=start, ends_at=end)
        problem_data = deepcopy(DEFAULT_PROBLEM_DATA)
        problem_data.update({"_id": "A", "contest": self.contest, "created_by": self.teacher,
                             "rule_type": "OI", "total_score": 10})
        problem_data.pop("tags")
        self.problem = Problem.objects.create(**problem_data)
        self.question = ClassroomQuestion.objects.create(quiz=self.quiz, problem=self.problem, slug="a",
                                                         title="C lab", kind=ClassroomQuestion.C_LAB,
                                                         points=10, published=True)
        self.first_submission = Submission.objects.create(
            problem=self.problem, contest=self.contest, user_id=self.first_student.id,
            username=self.first_student.username, language="C", code="first private code",
            info={"hidden_test": "private input"})
        self.second_submission = Submission.objects.create(
            problem=self.problem, contest=self.contest, user_id=self.second_student.id,
            username=self.second_student.username, language="C", code="second private code")
        self.detail_url = self.reverse("submission_api")
        self.list_url = self.reverse("contest_submission_list_api")
        self.rank_url = self.reverse("contest_rank_api")

    def test_students_see_only_their_own_code_and_submissions(self):
        self.client.login(username="student-one", password="student-pass")
        own = self.client.get(self.detail_url, {"id": self.first_submission.id})
        self.assertSuccess(own)
        self.assertEqual(own.data["data"]["code"], "first private code")
        self.assertNotIn("info", own.data["data"])
        self.assertFalse(own.data["data"]["can_unshare"])
        other = self.client.get(self.detail_url, {"id": self.second_submission.id})
        self.assertFailed(other)
        listing = self.client.get(self.list_url, {"contest_id": self.contest.id, "limit": 10})
        self.assertSuccess(listing)
        self.assertEqual([item["id"] for item in listing.data["data"]["results"]],
                         [self.first_submission.id])

    def test_student_can_read_own_history_after_exam_ends(self):
        self.contest.end_time = timezone.now() - timedelta(seconds=1)
        self.contest.save(update_fields=["end_time"])
        self.client.login(username="student-one", password="student-pass")
        listing = self.client.get(self.list_url, {"contest_id": self.contest.id, "limit": 10})
        self.assertSuccess(listing)
        self.assertEqual(listing.data["data"]["total"], 1)
        detail = self.client.get(self.detail_url, {"id": self.first_submission.id})
        self.assertSuccess(detail)

    def test_students_cannot_share_code_or_view_rank(self):
        self.client.login(username="student-one", password="student-pass")
        sharing = self.client.put(self.detail_url, {"id": self.first_submission.id, "shared": True})
        self.assertFailed(sharing)
        self.first_submission.refresh_from_db()
        self.assertFalse(self.first_submission.shared)
        ranking = self.client.get(self.rank_url, {"contest_id": self.contest.id})
        self.assertFailed(ranking)

    def test_unenrolled_account_cannot_submit_or_list(self):
        self.client.login(username="outsider", password="student-pass")
        listing = self.client.get(self.list_url, {"contest_id": self.contest.id, "limit": 10})
        self.assertFailed(listing)
        with mock.patch("submission.views.oj.judge_task.send") as judge_task:
            submitted = self.client.post(self.detail_url, {"contest_id": self.contest.id,
                                                           "problem_id": self.problem.id,
                                                           "language": "C", "code": "int main(void) { return 0; }"})
        self.assertFailed(submitted)
        judge_task.assert_not_called()

    def test_teacher_can_review_classroom_submissions_and_rank(self):
        self.client.login(username="teacher", password="teacher-pass")
        listing = self.client.get(self.list_url, {"contest_id": self.contest.id, "limit": 10})
        self.assertSuccess(listing)
        self.assertEqual(listing.data["data"]["total"], 2)
        detail = self.client.get(self.detail_url, {"id": self.second_submission.id})
        self.assertSuccess(detail)
        ranking = self.client.get(self.rank_url, {"contest_id": self.contest.id})
        self.assertSuccess(ranking)

    def test_classroom_problem_omits_hints_and_hidden_judge_data(self):
        self.problem.hint = "private hint"
        self.problem.source = "private source"
        self.problem.template = {"C": "private template"}
        self.problem.save(update_fields=["hint", "source", "template"])
        self.client.login(username="student-one", password="student-pass")
        url = self.reverse("contest_problem_api")
        response = self.client.get(url, {"contest_id": self.contest.id, "problem_id": self.problem._id})
        self.assertSuccess(response)
        problem = response.data["data"]
        for key in ("hint", "source", "template", "test_case_id", "test_case_score",
                    "spj_code", "submission_number", "accepted_number", "statistic_info"):
            self.assertNotIn(key, problem)
        self.assertEqual(problem["title"], self.problem.title)

    def test_unpublished_question_hidden_from_stock_problem_and_submission_apis(self):
        hidden_data = deepcopy(DEFAULT_PROBLEM_DATA)
        hidden_data.update({"_id": "B", "contest": self.contest, "created_by": self.teacher,
                            "rule_type": "OI", "total_score": 10})
        hidden_data.pop("tags")
        hidden_problem = Problem.objects.create(**hidden_data)
        ClassroomQuestion.objects.create(quiz=self.quiz, problem=hidden_problem, slug="b",
                                         title="Hidden C lab", kind=ClassroomQuestion.C_LAB,
                                         points=10, published=False)
        self.client.login(username="student-one", password="student-pass")
        url = self.reverse("contest_problem_api")
        listing = self.client.get(url, {"contest_id": self.contest.id})
        self.assertSuccess(listing)
        self.assertEqual([item["id"] for item in listing.data["data"]], [self.problem.id])
        detail = self.client.get(url, {"contest_id": self.contest.id, "problem_id": "B"})
        self.assertFailed(detail)
        with mock.patch("submission.views.oj.SubmissionAPI.throttling", return_value=None), \
                mock.patch("submission.views.oj.judge_task.send") as judge_task:
            submitted = self.client.post(self.detail_url, {"contest_id": self.contest.id,
                                                           "problem_id": hidden_problem.id,
                                                           "language": "C", "code": "int main(void) {return 0;}"})
        self.assertFailed(submitted)
        self.assertEqual(submitted.data["data"], "Problem not exist")
        judge_task.assert_not_called()

    def test_stock_submission_requires_open_quiz_time(self):
        self.quiz.ends_at = timezone.now() - timedelta(seconds=1)
        self.quiz.save(update_fields=["ends_at"])
        self.client.login(username="student-one", password="student-pass")
        problem = self.client.get(self.reverse("contest_problem_api"),
                                  {"contest_id": self.contest.id, "problem_id": self.problem._id})
        self.assertFailed(problem)
        with mock.patch("submission.views.oj.SubmissionAPI.throttling", return_value=None), \
                mock.patch("submission.views.oj.judge_task.send") as judge_task:
            submitted = self.client.post(self.detail_url, {"contest_id": self.contest.id,
                                                           "problem_id": self.problem.id,
                                                           "language": "C", "code": "int main(void) {return 0;}"})
        self.assertFailed(submitted)
        self.assertEqual(submitted.data["data"], "Quiz is unavailable")
        judge_task.assert_not_called()

    def test_unpublished_quiz_blocks_stock_problem_and_submission_apis(self):
        self.quiz.published = False
        self.quiz.save(update_fields=["published"])
        self.client.login(username="student-one", password="student-pass")
        problem = self.client.get(self.reverse("contest_problem_api"),
                                  {"contest_id": self.contest.id, "problem_id": self.problem._id})
        self.assertFailed(problem)
        with mock.patch("submission.views.oj.SubmissionAPI.throttling", return_value=None), \
                mock.patch("submission.views.oj.judge_task.send") as judge_task:
            submitted = self.client.post(self.detail_url, {"contest_id": self.contest.id,
                                                           "problem_id": self.problem.id,
                                                           "language": "C", "code": "int main(void) {return 0;}"})
        self.assertFailed(submitted)
        judge_task.assert_not_called()

    def test_published_c_lab_can_be_submitted(self):
        self.client.login(username="student-one", password="student-pass")
        source = "int main(void) {return 0;}\n"
        with mock.patch("submission.views.oj.SubmissionAPI.throttling", return_value=None), \
                mock.patch("submission.views.oj.judge_task.send") as judge_task:
            submitted = self.client.post(self.detail_url, {"contest_id": self.contest.id,
                                                           "problem_id": self.problem.id,
                                                           "language": "C", "code": source})
        self.assertSuccess(submitted)
        saved = Submission.objects.get(id=submitted.data["data"]["submission_id"], user_id=self.first_student.id)
        self.assertEqual(saved.code, source)
        judge_task.assert_called_once()

    def test_native_contest_editor_cannot_change_classroom_timing(self):
        self.client.login(username="teacher", password="teacher-pass")
        original_end = self.contest.end_time
        payload = {"id": self.contest.id, "title": "Changed title", "description": "",
                   "start_time": self.contest.start_time.isoformat(),
                   "end_time": (original_end + timedelta(hours=1)).isoformat(),
                   "password": "", "visible": True, "real_time_rank": False,
                   "allowed_ip_ranges": []}
        response = self.client.put(self.reverse("contest_admin_api"), payload)
        self.assertFailed(response)
        self.contest.refresh_from_db()
        self.assertEqual(self.contest.end_time, original_end)

    def test_classroom_contest_hidden_from_outsiders(self):
        self.client.login(username="outsider", password="student-pass")
        detail = self.client.get(self.reverse("contest_api"), {"id": self.contest.id})
        self.assertFailed(detail)
        listing = self.client.get(self.reverse("contest_list_api"), {"limit": 10})
        self.assertSuccess(listing)
        self.assertEqual(listing.data["data"]["total"], 0)

    def test_classroom_only_mode_hides_shared_public_submissions(self):
        problem_data = deepcopy(DEFAULT_PROBLEM_DATA)
        problem_data.update({"_id": "public-c", "created_by": self.teacher,
                             "share_submission": True})
        problem_data.pop("tags")
        public_problem = Problem.objects.create(**problem_data)
        own = Submission.objects.create(problem=public_problem, user_id=self.first_student.id,
                                        username=self.first_student.username, language="C", code="own")
        other = Submission.objects.create(problem=public_problem, user_id=self.second_student.id,
                                          username=self.second_student.username, language="C", code="private",
                                          shared=True)
        self.client.login(username="student-one", password="student-pass")
        with mock.patch.dict(os.environ, {"FCU_CLASSROOM_ONLY": "true"}):
            listing = self.client.get(self.reverse("submission_list_api"), {"limit": 10})
            self.assertSuccess(listing)
            self.assertEqual([item["id"] for item in listing.data["data"]["results"]], [own.id])
            detail = self.client.get(self.detail_url, {"id": other.id})
            self.assertFailed(detail)

    def test_classroom_only_mode_hides_stock_contest_rank(self):
        ordinary = Contest.objects.create(
            title="Ordinary QDU contest", description="", real_time_rank=True, password=None,
            rule_type="OI", start_time=self.contest.start_time, end_time=self.contest.end_time,
            created_by=self.teacher, visible=True, allowed_ip_ranges=[])
        self.client.login(username="student-one", password="student-pass")
        with mock.patch.dict(os.environ, {"FCU_CLASSROOM_ONLY": "true"}):
            ranking = self.client.get(self.rank_url, {"contest_id": ordinary.id})
            self.assertFailed(ranking)
        with mock.patch.dict(os.environ, {"FCU_CLASSROOM_ONLY": "false"}):
            ranking = self.client.get(self.rank_url, {"contest_id": ordinary.id})
            self.assertSuccess(ranking)
