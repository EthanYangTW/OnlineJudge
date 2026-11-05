import json
from datetime import timedelta
from unittest.mock import patch

from django.core.cache import cache
from django.test import Client, RequestFactory, TestCase
from django.utils import timezone

from account.models import AdminType, ProblemPermission, User
from contest.models import Contest
from problem.models import Problem, ProblemRuleType, ProblemTag
from submission.models import JudgeStatus, Submission

from .access import client_ip, login_failure_key
from .models import Classroom, ClassroomAttempt, ClassroomQuestion, ClassroomScore, Enrollment


class ClassroomAPITest(TestCase):
    def setUp(self):
        # The rate limiter uses shared Redis while test database rows are rolled
        # back between cases. Remove only this fixture's keys for test isolation.
        for username in ("D1234567", "D7654321", "teacher"):
            for address in ("192.168.1.5", "192.168.1.6", "192.168.1.7"):
                request = RequestFactory().get("/", REMOTE_ADDR=address)
                cache.delete(login_failure_key(request, username))
        self.teacher = User.objects.create(username="teacher", admin_type=AdminType.SUPER_ADMIN,
                                           problem_permission=ProblemPermission.ALL)
        self.teacher.set_password("a-long-secret-password")
        self.teacher.save()
        self.teacher_client = Client()
        self.teacher_client.force_login(self.teacher)
        self.base = "/api/fcu/classroom/"
        response = self.post(self.teacher_client, "classes/", {"slug": "c-101", "name": "C 101"})
        self.assertEqual(response.status_code, 201)
        self.cls = Classroom.objects.get(slug="c-101")
        response = self.post(self.teacher_client, "classes/c-101/roster/",
                             {"csv": "login,name,class,password\nD1234567,Alice,A,D1234567\nD7654321,Bob,A,D7654321\n"})
        self.assertEqual(response.status_code, 200)
        self.alice = User.objects.get(username="D1234567")
        self.bob = User.objects.get(username="D7654321")

    def post(self, client, path, data, ip="192.168.1.5"):
        return client.post(self.base + path, data=json.dumps(data), content_type="application/json",
                           REMOTE_ADDR=ip)

    def put(self, client, path, data, ip="192.168.1.5"):
        return client.put(self.base + path, data=json.dumps(data), content_type="application/json",
                          REMOTE_ADDR=ip)

    def login(self, username, ip):
        client = Client()
        response = self.post(client, "login/", {"class": "c-101", "login": username,
                                                "password": username.upper()}, ip=ip)
        return client, response

    def test_student_login_finds_class_without_code(self):
        client = Client()
        response = self.post(client, "login/", {"login": "D1234567", "password": "D1234567"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["data"]["class"], "c-101")
        self.assertEqual(client.get(self.base + "classes/c-101/state/", REMOTE_ADDR="192.168.1.5").status_code, 200)

    def test_classless_login_rejects_ambiguous_enrollment(self):
        second = Classroom.objects.create(slug="c-102", name="C 102", teacher=self.teacher)
        Enrollment.objects.create(classroom=second, user=self.alice)
        response = self.post(Client(), "login/", {"login": "D1234567", "password": "D1234567"})
        self.assertEqual(response.status_code, 401)

    def quiz(self, mode="exam"):
        starts = timezone.now() - timedelta(minutes=1)
        payload = {"slug": mode + "-1", "title": mode.title(), "mode": mode,
                   "startsAt": starts.isoformat(), "published": True}
        if mode == "exam":
            payload["endsAt"] = (timezone.now() + timedelta(minutes=20)).isoformat()
        response = self.post(self.teacher_client, "classes/c-101/quizzes/", payload)
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]

    def question(self, quiz, slug="choice-1"):
        path = "classes/c-101/quizzes/{}/questions/".format(quiz["id"])
        response = self.post(self.teacher_client, path,
                             {"slug": slug, "title": "Two plus two", "kind": "mcq",
                              "prompt": "What is 2 + 2?", "points": 7,
                              "choices": ["3", "4", "5"], "answerIndex": 1})
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["data"]

    def test_roster_is_hashed_and_ip_lock_can_be_reset_or_disabled(self):
        self.assertNotEqual(self.alice.password, "D1234567")
        self.assertTrue(self.alice.check_password("D1234567"))
        first_client, first = self.login("d1234567", "192.168.1.5")
        self.assertEqual(first.status_code, 200)
        same_roster = {"csv": "login,name,class,password\nD1234567,Alice,A,D1234567\n"}
        self.assertEqual(self.post(self.teacher_client, "classes/c-101/roster/", same_roster).status_code, 200)
        self.assertEqual(Enrollment.objects.get(classroom=self.cls, user=self.alice).locked_ip, "192.168.1.5")
        self.assertEqual(first_client.get(self.base + "classes/c-101/state/",
                                          REMOTE_ADDR="192.168.1.5").status_code, 200)
        _, second = self.login("d1234567", "192.168.1.6")
        self.assertEqual(second.status_code, 403)
        stock_second = Client().post("/api/login", data=json.dumps({"username": "D1234567",
                                                                    "password": "D1234567"}),
                                     content_type="application/json", REMOTE_ADDR="192.168.1.6")
        self.assertIsNotNone(stock_second.data["error"])
        response = self.post(self.teacher_client, "classes/c-101/ip-lock/",
                             {"action": "reset-student", "login": "d1234567"})
        self.assertEqual(response.status_code, 200)
        _, after_reset = self.login("d1234567", "192.168.1.6")
        self.assertEqual(after_reset.status_code, 200)
        response = self.post(self.teacher_client, "classes/c-101/ip-lock/",
                             {"action": "set-student", "login": "d1234567", "disabled": True})
        self.assertEqual(response.status_code, 200)
        _, unlocked = self.login("d1234567", "192.168.1.5")
        self.assertEqual(unlocked.status_code, 200)
        self.assertTrue(Enrollment.objects.get(classroom=self.cls, user=self.alice).ip_lock_disabled)

    def test_exam_mcq_is_private_scored_and_single_attempt(self):
        quiz = self.quiz()
        question = self.question(quiz)
        alice_client, response = self.login("d1234567", "192.168.1.5")
        self.assertEqual(response.status_code, 200)
        bob_client, response = self.login("d7654321", "192.168.1.6")
        self.assertEqual(response.status_code, 200)
        state = alice_client.get(self.base + "classes/c-101/state/", REMOTE_ADDR="192.168.1.5")
        self.assertEqual(state.status_code, 200)
        self.assertNotIn("answerIndex", json.dumps(state.json()["data"]))
        self.assertEqual(state.json()["data"]["quizzes"][0]["questions"][0]["points"], 7)
        path = "classes/c-101/quizzes/{}/questions/{}/submit/".format(quiz["id"], question["id"])
        result = self.post(alice_client, path, {"choiceIndex": 1})
        self.assertEqual(result.status_code, 201, result.content)
        self.assertEqual(result.json()["data"]["earned"], 7)
        self.assertEqual(self.post(alice_client, path, {"choiceIndex": 0}).status_code, 409)
        self.assertEqual(ClassroomScore.objects.get(question_id=question["id"], user=self.alice).best_earned, 7)
        self.assertEqual(bob_client.get(self.base + "classes/c-101/scores/",
                                        REMOTE_ADDR="192.168.1.6").status_code, 403)
        self.assertEqual(bob_client.get(self.base + "classes/c-101/quizzes/{}/submissions/?login=d1234567".format(
            quiz["id"]), REMOTE_ADDR="192.168.1.6").status_code, 403)
        teacher_scores = self.teacher_client.get(self.base + "classes/c-101/scores/").json()["data"]
        self.assertEqual(teacher_scores[0]["scores"][str(question["id"])]["best"], 7)
        question_path = "classes/c-101/quizzes/{}/questions/{}/".format(quiz["id"], question["id"])
        edit = {"slug": "choice-1", "title": "Two plus two", "kind": "mcq", "points": 14,
                "prompt": "What is 2 + 2?", "choices": ["3", "4", "5"], "answerIndex": 1}
        self.assertEqual(self.put(self.teacher_client, question_path, edit).status_code, 200)
        score = ClassroomScore.objects.get(question_id=question["id"], user=self.alice)
        self.assertEqual((score.best_earned, score.latest_earned), (14, 14))
        self.assertEqual(ClassroomAttempt.objects.get(question_id=question["id"], user=self.alice).earned, 14)
        self.assertEqual(self.put(self.teacher_client,
                                  question_path, {**edit, "title": "Changed"}).status_code, 409)
        self.assertEqual(self.put(self.teacher_client, "classes/c-101/quizzes/{}/".format(quiz["id"]),
                                  {"endsAt": (timezone.now() - timedelta(seconds=1)).isoformat()}).status_code, 200)
        self.assertEqual(self.post(bob_client, path, {"choiceIndex": 1}, ip="192.168.1.6").status_code, 403)

    def test_practice_keeps_best_and_latest_score_separately(self):
        quiz = self.quiz("practice")
        question = self.question(quiz)
        alice_client, response = self.login("d1234567", "192.168.1.5")
        self.assertEqual(response.status_code, 200)
        path = "classes/c-101/quizzes/{}/questions/{}/submit/".format(quiz["id"], question["id"])
        self.assertEqual(self.post(alice_client, path, {"choiceIndex": 1}).status_code, 201)
        self.assertEqual(self.post(alice_client, path, {"choiceIndex": 0}).status_code, 201)
        score = ClassroomScore.objects.get(question_id=question["id"], user=self.alice)
        self.assertEqual((score.best_earned, score.latest_earned, score.attempts), (7, 0, 2))
        history = alice_client.get(self.base + "classes/c-101/quizzes/{}/submissions/".format(quiz["id"]),
                                   REMOTE_ADDR="192.168.1.5").json()["data"]
        self.assertEqual(len(history), 2)

    def test_c_submission_source_and_score_are_saved(self):
        quiz = self.quiz()
        contest = Contest.objects.get(id=quiz["contestId"])
        problem = Problem.objects.create(_id="A", contest=contest, title="Add", description="Add two integers",
                                         input_description="Two integers", output_description="Their sum",
                                         samples=[{"input": "1", "output": "1"}],
                                         test_case_id="cases", test_case_score=[{"input_name": "1.in", "output_name": "1.out", "score": 10}],
                                         languages=["C"], template={}, created_by=self.teacher,
                                         time_limit=1000, memory_limit=128, rule_type=ProblemRuleType.OI,
                                         difficulty="Low", total_score=10)
        problem.tags.add(ProblemTag.objects.create(name="basic"))
        response = self.post(self.teacher_client,
                             "classes/c-101/quizzes/{}/questions/".format(quiz["id"]),
                             {"slug": "add", "title": "Add", "kind": "c_lab", "points": 20,
                              "prompt": "Add two integers", "problemId": problem.id})
        self.assertEqual(response.status_code, 201, response.content)
        question = ClassroomQuestion.objects.get(id=response.json()["data"]["id"])
        make_public = self.teacher_client.post("/api/admin/contest_problem/make_public/",
                                               data=json.dumps({"id": problem.id, "display_id": "PUBLIC-A"}),
                                               content_type="application/json")
        self.assertIsNotNone(make_public.data["error"])
        self.assertFalse(Problem.objects.filter(_id="PUBLIC-A", contest__isnull=True).exists())
        problem.refresh_from_db()
        self.assertFalse(problem.is_public)
        submission = Submission.objects.create(contest=contest, problem=problem, user_id=self.alice.id,
                                               username=self.alice.username, code="int main(){return 0;}",
                                               language="C", result=JudgeStatus.PENDING)
        self.assertEqual(ClassroomAttempt.objects.get(submission=submission).verdict, str(JudgeStatus.PENDING))
        self.assertFalse(ClassroomScore.objects.filter(question=question, user=self.alice).exists())
        submission.result = JudgeStatus.PARTIALLY_ACCEPTED
        submission.statistic_info = {"score": 5}
        submission.save()
        attempt = ClassroomAttempt.objects.get(submission=submission)
        self.assertEqual(attempt.earned, 10)
        self.assertEqual(ClassroomScore.objects.get(question=question, user=self.alice).best_earned, 10)
        second = Submission.objects.create(contest=contest, problem=problem, user_id=self.alice.id,
                                           username=self.alice.username, code="int main(){return 0;}",
                                           language="C", result=JudgeStatus.PENDING)
        second.result = JudgeStatus.PARTIALLY_ACCEPTED
        second.statistic_info = {"score": 2}
        second.save()
        score = ClassroomScore.objects.get(question=question, user=self.alice)
        self.assertEqual((score.best_earned, score.latest_earned, score.attempts), (10, 4, 2))
        edit_question = {"slug": "add", "title": "Add", "kind": "c_lab", "points": 30,
                         "prompt": "Add two integers", "problemId": problem.id}
        question_path = "classes/c-101/quizzes/{}/questions/{}/".format(quiz["id"], question.id)
        self.assertEqual(self.put(self.teacher_client, question_path, edit_question).status_code, 200)
        self.assertEqual(ClassroomAttempt.objects.get(submission=submission).earned, 15)
        self.assertEqual(ClassroomAttempt.objects.get(submission=second).earned, 6)
        score = ClassroomScore.objects.get(question=question, user=self.alice)
        self.assertEqual((score.best_earned, score.latest_earned, score.attempts), (15, 6, 2))
        problem_edit = {"id": problem.id, "contest_id": contest.id, "_id": "A", "title": "Corrected title",
                        "description": "Clarified instructions", "input_description": "Two integers",
                        "output_description": "Their sum",
                        "samples": [{"input": "1", "output": "1"}], "test_case_id": "cases",
                        "test_case_score": [{"input_name": "1.in", "output_name": "1.out", "score": 10}],
                        "time_limit": 1000, "memory_limit": 128, "languages": ["C"], "template": {},
                        "rule_type": ProblemRuleType.OI, "io_mode": problem.io_mode,
                        "spj": False, "spj_language": None, "spj_code": None, "spj_compile_ok": False,
                        "visible": True, "difficulty": "Low", "tags": ["basic"],
                        "hint": None, "source": None, "share_submission": False}
        native_path = "/api/admin/contest/problem"
        allowed = self.teacher_client.put(native_path, data=json.dumps(problem_edit),
                                          content_type="application/json")
        self.assertIsNone(allowed.data["error"], allowed.data)
        problem.refresh_from_db()
        self.assertEqual(problem.title, "Corrected title")
        rejected = self.teacher_client.put(native_path,
                                           data=json.dumps({**problem_edit, "test_case_score": [
                                               {"input_name": "1.in", "output_name": "1.out", "score": 20}]}),
                                           content_type="application/json")
        self.assertIsNotNone(rejected.data["error"])
        problem.refresh_from_db()
        self.assertEqual(problem.total_score, 10)
        alice_client, _ = self.login("d1234567", "192.168.1.5")
        history = alice_client.get(self.base + "classes/c-101/quizzes/{}/submissions/".format(quiz["id"]),
                                   REMOTE_ADDR="192.168.1.5").json()["data"]
        self.assertEqual(history[0]["code"], "int main(){return 0;}")
        check_path = "classes/c-101/quizzes/{}/questions/{}/check/".format(quiz["id"], question.id)
        with patch.dict("os.environ", {"FCU_SYNTAX_SERVER_URL": "http://syntax-only:8080/"}), \
                patch("fcu_classroom.views.requests.post") as send:
            send.return_value.json.return_value = {
                "err": None, "data": {"ok": False,
                                      "diagnostics": [{"line": 2, "column": 5, "severity": "error",
                                                       "message": "expected ';' before '}'"}]}}
            result = self.post(alice_client, check_path, {"code": "int main(){ bad }"})
            self.assertEqual(result.status_code, 200, result.content)
            self.assertFalse(result.json()["data"]["ok"])
            self.assertEqual(result.json()["data"]["diagnostics"][0]["line"], 2)
            sent = send.call_args.kwargs["json"]
            self.assertEqual(sent, {"src": "int main(){ bad }"})
            self.assertTrue(send.call_args.args[0].endswith("/check_c_syntax"))
            self.assertEqual(self.post(alice_client, check_path, {"code": "int main(){}"}).status_code, 429)
        with patch.dict("os.environ", {"FCU_SYNTAX_SERVER_URL": ""}), \
                patch("fcu_classroom.views.requests.post") as send:
            self.assertEqual(self.post(alice_client, check_path, {"code": "int main(){}"}).status_code, 503)
            send.assert_not_called()

    def test_custom_input_run_uses_private_sandbox_and_never_scores(self):
        quiz = self.quiz()
        contest = Contest.objects.get(id=quiz["contestId"])
        problem = Problem.objects.create(
            _id="A", contest=contest, title="Echo", description="Echo input",
            input_description="An integer", output_description="The integer",
            samples=[{"input": "2", "output": "2"}], test_case_id="cases-run",
            test_case_score=[{"input_name": "1.in", "output_name": "1.out", "score": 10}],
            languages=["C"], template={}, created_by=self.teacher, time_limit=1000,
            memory_limit=128, rule_type=ProblemRuleType.OI, difficulty="Low", total_score=10)
        question = self.post(self.teacher_client,
                             "classes/c-101/quizzes/{}/questions/".format(quiz["id"]),
                             {"slug": "echo", "title": "Echo", "kind": "c_lab", "points": 10,
                              "prompt": "Echo input", "problemId": problem.id}).json()["data"]
        student, login = self.login("d1234567", "192.168.1.5")
        self.assertEqual(login.status_code, 200)
        path = "classes/c-101/quizzes/{}/questions/{}/run/".format(quiz["id"], question["id"])
        source = '#include <stdio.h>\nint main(void){int x;scanf("%d",&x);printf("%d\\n",x);}\n'
        with patch.dict("os.environ", {"FCU_SYNTAX_SERVER_URL": "http://syntax-only:8080/"}), \
                patch("fcu_classroom.views.requests.post") as send:
            send.return_value.json.return_value = {
                "err": None, "data": {"status": "ok", "stdout": "7\n", "stderr": "",
                                      "exitCode": 0, "timeMs": 12}}
            result = self.post(student, path, {"code": source, "input": "7\n"})
            self.assertEqual(result.status_code, 200, result.content)
            self.assertEqual(result.json()["data"], {"status": "ok", "stdout": "7\n", "stderr": "",
                                                     "exitCode": 0, "timeMs": 12})
            self.assertEqual(send.call_args.kwargs["json"], {"src": source, "input": "7\n"})
            self.assertTrue(send.call_args.args[0].endswith("/run_c"))
            self.assertFalse(ClassroomAttempt.objects.filter(question_id=question["id"]).exists())
            self.assertFalse(ClassroomScore.objects.filter(question_id=question["id"]).exists())
            self.assertEqual(self.post(student, path, {"code": source, "input": "x" * 32001}).status_code, 400)
            self.assertEqual(self.put(self.teacher_client, "classes/c-101/quizzes/{}/".format(quiz["id"]),
                                      {"endsAt": (timezone.now() - timedelta(seconds=1)).isoformat()}).status_code,
                             200)
            send.reset_mock()
            self.assertEqual(self.post(student, path, {"code": source, "input": "7\n"}).status_code, 403)
            send.assert_not_called()
            self.assertEqual(self.post(self.teacher_client, path, {"code": source, "input": "7\n"}).status_code,
                             200)

    def test_forwarded_ip_only_from_configured_proxy(self):
        factory = RequestFactory()
        request = factory.get("/", REMOTE_ADDR="192.168.1.5", HTTP_X_REAL_IP="203.0.113.7")
        self.assertEqual(client_ip(request), "192.168.1.5")
        request = factory.get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_REAL_IP="203.0.113.7")
        self.assertEqual(client_ip(request), "203.0.113.7")

    def test_stock_registration_rank_and_unenrolled_login_are_closed(self):
        registration_data = {"username": "newstudent", "password": "strongpass",
                             "email": "student@example.test", "captcha": "anything"}
        registration = Client().post(
            "/api/register", data=json.dumps(registration_data),
            content_type="application/json")
        self.assertIsNotNone(registration.data["error"])
        outsider = User.objects.create(username="outsider")
        outsider.set_password("outsider-password")
        outsider.save()
        login = Client().post("/api/login", data=json.dumps({"username": "outsider",
                                                             "password": "outsider-password"}),
                              content_type="application/json", REMOTE_ADDR="192.168.1.20")
        self.assertIsNotNone(login.data["error"])
        self.assertIsNone(User.objects.filter(username="newstudent").first())
        rank = Client().get("/api/user_rank")
        self.assertIsNotNone(rank.data["error"])

    def test_stock_and_classroom_logins_share_rate_limit(self):
        for _ in range(5):
            response = Client().post("/api/login", data=json.dumps({"username": "D7654321",
                                                                    "password": "wrong-password"}),
                                     content_type="application/json", REMOTE_ADDR="192.168.1.6")
            self.assertIsNotNone(response.data["error"])
        blocked = Client().post("/api/login", data=json.dumps({"username": "D7654321",
                                                               "password": "D7654321"}),
                                content_type="application/json", REMOTE_ADDR="192.168.1.6")
        self.assertEqual(blocked.data["error"], "throttled")
        _, classroom_blocked = self.login("d7654321", "192.168.1.6")
        self.assertEqual(classroom_blocked.status_code, 429)
        _, other_ip = self.login("d7654321", "192.168.1.7")
        self.assertEqual(other_ip.status_code, 200)

    def test_classroom_login_does_not_bypass_two_factor_auth(self):
        self.teacher.two_factor_auth = True
        self.teacher.tfa_token = "test-token"
        self.teacher.save(update_fields=["two_factor_auth", "tfa_token"])
        response = self.post(Client(), "login/", {"class": "c-101", "login": "teacher",
                                                  "password": "a-long-secret-password"})
        self.assertEqual(response.status_code, 401)
