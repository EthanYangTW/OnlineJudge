"""Practice peer activity is visible without exposing exam work or code."""

import json
from datetime import timedelta

from django.test import Client, TestCase
from django.utils import timezone

from account.models import AdminType, ProblemPermission, User
from problem.models import Problem, ProblemRuleType
from submission.models import JudgeStatus, Submission

from .models import Classroom, ClassroomQuestion, ClassroomQuiz, Enrollment


class PracticeSocialTest(TestCase):
    def setUp(self):
        self.teacher = User.objects.create(username="teacher-social", admin_type=AdminType.SUPER_ADMIN,
                                           problem_permission=ProblemPermission.ALL)
        self.teacher_client = Client()
        self.teacher_client.force_login(self.teacher)
        self.base = "/api/fcu/classroom/classes/social-class/"
        self._post(self.teacher_client, "/api/fcu/classroom/classes/",
                   {"slug": "social-class", "name": "Social class"})
        roster = "login,name,class,password\nS00001,Alice,Lab,S00001\nS00002,Bob,Lab,S00002\n"
        response = self._post(self.teacher_client, self.base + "roster/", {"csv": roster})
        self.assertEqual(response.status_code, 200, response.content)
        self.alice = User.objects.get(username="S00001")
        self.bob = User.objects.get(username="S00002")
        self.alice_client = Client()
        self.alice_client.force_login(self.alice)
        self.bob_client = Client()
        self.bob_client.force_login(self.bob)
        starts = timezone.now() - timedelta(minutes=2)
        self.practice = self._quiz("practice", starts)
        self.exam = self._quiz("exam", starts, timezone.now() + timedelta(minutes=20))
        self.choice = self._choice(self.practice, "practice-choice")
        self.exam_choice = self._choice(self.exam, "exam-choice")

    def _post(self, client, path, data):
        return client.post(path, data=json.dumps(data), content_type="application/json",
                           REMOTE_ADDR="192.168.1.5")

    def _quiz(self, mode, starts, ends=None, slug=None):
        payload = {"slug": slug or mode, "title": mode.title(), "mode": mode, "published": True,
                   "startsAt": starts.isoformat()}
        if ends is not None:
            payload["endsAt"] = ends.isoformat()
        response = self._post(self.teacher_client, self.base + "quizzes/", payload)
        self.assertEqual(response.status_code, 201, response.content)
        return ClassroomQuiz.objects.get(id=response.json()["data"]["id"])

    def _choice(self, quiz, slug):
        payload = {"slug": slug, "title": "Choose one", "kind": "mcq", "points": 7,
                   "prompt": "Which option?", "choices": ["wrong", "right"], "answerIndex": 1}
        response = self._post(self.teacher_client, self.base + "quizzes/{}/questions/".format(quiz.id), payload)
        self.assertEqual(response.status_code, 201, response.content)
        return ClassroomQuestion.objects.get(id=response.json()["data"]["id"])

    def _peer_path(self, quiz, endpoint, query=""):
        return self.base + "quizzes/{}/{}/".format(quiz.id, endpoint) + query

    def test_practice_shows_peer_scores_and_status_without_code_or_answers(self):
        submit_path = self.base + "quizzes/{}/questions/{}/submit/".format(
            self.practice.id, self.choice.id)
        self.assertEqual(self._post(self.alice_client, submit_path, {"choiceIndex": 1}).status_code, 201)
        self.assertEqual(self._post(self.bob_client, submit_path, {"choiceIndex": 0}).status_code, 201)
        self.assertEqual(self._post(self.alice_client,
                                    self.base + "quizzes/{}/questions/{}/submit/".format(
                                        self.exam.id, self.exam_choice.id), {"choiceIndex": 1}).status_code, 201)

        problem = Problem.objects.create(
            _id="A", contest=self.practice.contest, title="Add", description="Add",
            input_description="Two numbers", output_description="Sum", samples=[{"input": "1 2", "output": "3"}],
            test_case_id="cases", test_case_score=[{"input_name": "1.in", "output_name": "1.out", "score": 10}],
            languages=["C"], template={}, created_by=self.teacher, time_limit=1000, memory_limit=128,
            rule_type=ProblemRuleType.OI, difficulty="Low", total_score=10)
        lab = ClassroomQuestion.objects.create(quiz=self.practice, problem=problem, slug="add", title="Add",
                                               prompt="Add two", kind=ClassroomQuestion.C_LAB, points=10)
        submission = Submission.objects.create(contest=self.practice.contest, problem=problem,
                                               user_id=self.alice.id, username=self.alice.username,
                                               code="SECRET_OWNER_C_CODE", language="C",
                                               result=JudgeStatus.PENDING)
        submission.result = JudgeStatus.ACCEPTED
        submission.statistic_info = {"score": 10}
        submission.save()

        leaderboard = self.bob_client.get(self._peer_path(self.practice, "leaderboard"),
                                          REMOTE_ADDR="192.168.1.5")
        self.assertEqual(leaderboard.status_code, 200, leaderboard.content)
        data = leaderboard.json()["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual([(row["login"], row["score"], row["maxScore"]) for row in data["entries"]],
                         [("S00001", 17, 17), ("S00002", 0, 17)])
        self.assertEqual(data["entries"][0]["name"], "Alice")
        self.assertEqual(data["entries"][0]["solved"], 2)
        self.assertNotIn("SECRET_OWNER_C_CODE", json.dumps(data))
        self.assertNotIn("answerIndex", json.dumps(data))

        status = self.bob_client.get(self._peer_path(self.practice, "status"),
                                     REMOTE_ADDR="192.168.1.5")
        self.assertEqual(status.status_code, 200, status.content)
        entries = status.json()["data"]["entries"]
        self.assertEqual(len(entries), 3)
        self.assertTrue(any(row["login"] == "S00001" and row["questionId"] == lab.id and
                            row["verdict"] == str(JudgeStatus.ACCEPTED) for row in entries))
        payload = json.dumps(status.json()["data"])
        for forbidden in ("SECRET_OWNER_C_CODE", "choiceIndex", "answerIndex", "lockedIp", "password"):
            self.assertNotIn(forbidden, payload)
        self.assertNotIn(str(self.exam_choice.id), [str(row["questionId"]) for row in entries])

        other_practice = self._quiz("practice", timezone.now() - timedelta(minutes=1), slug="practice-two")
        other_choice = self._choice(other_practice, "second-choice")
        self.assertEqual(self._post(self.bob_client,
                                    self.base + "quizzes/{}/questions/{}/submit/".format(
                                        other_practice.id, other_choice.id), {"choiceIndex": 1}).status_code, 201)
        overview = self.bob_client.get(self.base + "practice/leaderboard/", REMOTE_ADDR="192.168.1.5")
        self.assertEqual(overview.status_code, 200, overview.content)
        self.assertEqual([(row["login"], row["score"], row["maxScore"])
                          for row in overview.json()["data"]["entries"]],
                         [("S00001", 17, 24), ("S00002", 7, 24)])
        overview_status = self.bob_client.get(self.base + "practice/status/", REMOTE_ADDR="192.168.1.5")
        self.assertEqual(overview_status.status_code, 200, overview_status.content)
        overview_entries = overview_status.json()["data"]["entries"]
        self.assertEqual(len(overview_entries), 4)
        self.assertTrue(any(row["quizId"] == other_practice.id for row in overview_entries))
        self.assertFalse(any(row["quizId"] == self.exam.id for row in overview_entries))
        self.assertNotIn("SECRET_OWNER_C_CODE", json.dumps(overview_status.json()["data"]))

    def test_exam_cross_class_and_unpublished_practice_are_private(self):
        outsider = User.objects.create(username="outsider")
        another_class = Classroom.objects.create(slug="other-class", name="Other class", teacher=self.teacher)
        Enrollment.objects.create(classroom=another_class, user=outsider)
        outsider_client = Client()
        outsider_client.force_login(outsider)
        for endpoint in ("leaderboard", "status"):
            self.assertEqual(self.alice_client.get(self._peer_path(self.exam, endpoint),
                                                   REMOTE_ADDR="192.168.1.5").status_code, 403)
            self.assertEqual(outsider_client.get(self._peer_path(self.practice, endpoint),
                                                 REMOTE_ADDR="192.168.1.5").status_code, 403)
            self.assertEqual(outsider_client.get(self.base + "practice/{}/".format(endpoint),
                                                 REMOTE_ADDR="192.168.1.5").status_code, 403)
        self.practice.published = False
        self.practice.save(update_fields=["published"])
        self.assertEqual(self.alice_client.get(self._peer_path(self.practice, "leaderboard"),
                                               REMOTE_ADDR="192.168.1.5").status_code, 403)
        hidden_overview = self.alice_client.get(self.base + "practice/leaderboard/",
                                                REMOTE_ADDR="192.168.1.5").json()["data"]
        self.assertEqual(hidden_overview["entries"], [])
        self.assertEqual(hidden_overview["total"], 0)
        self.practice.published = True
        self.practice.starts_at = timezone.now() + timedelta(minutes=10)
        self.practice.save(update_fields=["published", "starts_at"])
        self.assertEqual(self.alice_client.get(self._peer_path(self.practice, "status"),
                                               REMOTE_ADDR="192.168.1.5").status_code, 403)

    def test_pagination_is_bounded_and_hides_unpublished_question(self):
        self.assertTrue(Enrollment.objects.filter(classroom=Classroom.objects.get(slug="social-class")).exists())
        self.choice.published = False
        self.choice.save(update_fields=["published"])
        page = self.alice_client.get(self._peer_path(self.practice, "leaderboard", "?page=2&pageSize=1"),
                                     REMOTE_ADDR="192.168.1.5")
        self.assertEqual(page.status_code, 200, page.content)
        self.assertEqual(page.json()["data"]["total"], 2)
        self.assertEqual(page.json()["data"]["entries"][0]["rank"], 2)
        self.assertEqual(page.json()["data"]["entries"][0]["maxScore"], 0)
        for query in ("?page=0", "?pageSize=101", "?page=nope", "?page=1001"):
            self.assertEqual(self.alice_client.get(self._peer_path(self.practice, "status", query),
                                                   REMOTE_ADDR="192.168.1.5").status_code, 400)
