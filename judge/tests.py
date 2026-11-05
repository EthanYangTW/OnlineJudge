from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

import requests
from django.test import SimpleTestCase
from django.utils import timezone

from .dispatcher import DispatcherBase, JudgeDispatcher, JUDGE_SERVER_TIMEOUT
from submission.models import JudgeStatus
from problem.models import ProblemRuleType
from utils.constants import ContestStatus


class JudgeReliabilityTests(SimpleTestCase):
    def make_dispatcher(self, contest=None):
        submission = SimpleNamespace(
            id="submission-1", user_id=1, problem_id=7, contest_id=contest.id if contest else None,
            language="C", code="int main(void) { return 0; }", info={}, statistic_info={},
            result=JudgeStatus.PENDING, create_time=timezone.now(), save=mock.Mock())
        problem = SimpleNamespace(
            id=7, rule_type=ProblemRuleType.OI, test_case_score=[{"score": 10}],
            template={}, spj_code=None, time_limit=1000, memory_limit=64,
            test_case_id="private-cases", spj_version=None, io_mode={"io_mode": "Standard IO"})
        dispatcher = JudgeDispatcher.__new__(JudgeDispatcher)
        dispatcher.submission = submission
        dispatcher.problem = problem
        dispatcher.contest = contest
        dispatcher.contest_id = submission.contest_id
        dispatcher.last_result = None
        dispatcher.token = "test-token"
        return dispatcher

    def run_response(self, dispatcher, response):
        options = SimpleNamespace(languages=[{"name": "C", "config": {}}], spj_languages=[])
        with mock.patch("judge.dispatcher.SysOptions", options), \
                mock.patch("judge.dispatcher.ChooseJudgeServer") as choose, \
                mock.patch("judge.dispatcher.Submission") as submission_model, \
                mock.patch.object(dispatcher, "_request", return_value=response), \
                mock.patch("judge.dispatcher.process_pending_task"), \
                mock.patch.object(dispatcher, "update_problem_status") as update_problem, \
                mock.patch.object(dispatcher, "update_contest_problem_status") as update_contest, \
                mock.patch.object(dispatcher, "update_contest_rank") as update_rank, \
                mock.patch("judge.dispatcher.User") as user_model, \
                mock.patch("judge.dispatcher.transaction.atomic"):
            choose.return_value.__enter__.return_value.service_url = "http://judge:8080/"
            user_model.objects.get.return_value.is_contest_admin.return_value = False
            dispatcher.judge()
            return submission_model, update_problem, update_contest, update_rank

    def test_request_has_finite_timeout(self):
        dispatcher = DispatcherBase.__new__(DispatcherBase)
        dispatcher.token = "test-token"
        with mock.patch("judge.dispatcher.requests.post") as post:
            post.return_value.json.return_value = {"err": None, "data": []}
            self.assertEqual(dispatcher._request("http://judge/judge", {"src": "C"}),
                             {"err": None, "data": []})
            self.assertEqual(post.call_args.kwargs["timeout"], JUDGE_SERVER_TIMEOUT)
            post.return_value.raise_for_status.assert_called_once()

    def test_transport_and_http_errors_are_not_compiler_errors(self):
        dispatcher = DispatcherBase.__new__(DispatcherBase)
        dispatcher.token = "test-token"
        for failure in (requests.Timeout(), requests.HTTPError()):
            with self.subTest(failure=type(failure).__name__), \
                    mock.patch("judge.dispatcher.requests.post") as post, \
                    mock.patch("judge.dispatcher.logger"):
                if isinstance(failure, requests.HTTPError):
                    post.return_value.raise_for_status.side_effect = failure
                else:
                    post.side_effect = failure
                self.assertIsNone(dispatcher._request("http://judge/judge"))

    def test_judge_service_error_never_updates_student_score(self):
        dispatcher = self.make_dispatcher()
        _, update_problem, update_contest, update_rank = self.run_response(
            dispatcher, {"err": "JudgeClientError", "data": "private server path"})
        self.assertEqual(dispatcher.submission.result, JudgeStatus.SYSTEM_ERROR)
        self.assertNotIn("private server path", str(dispatcher.submission.statistic_info))
        update_problem.assert_not_called()
        update_contest.assert_not_called()
        update_rank.assert_not_called()

    def test_compile_error_remains_a_student_verdict(self):
        dispatcher = self.make_dispatcher()
        _, update_problem, _, _ = self.run_response(
            dispatcher, {"err": "CompileError", "data": "answer.c:1: error"})
        self.assertEqual(dispatcher.submission.result, JudgeStatus.COMPILE_ERROR)
        update_problem.assert_called_once()

    def test_empty_test_results_are_system_error(self):
        dispatcher = self.make_dispatcher()
        _, update_problem, _, _ = self.run_response(dispatcher, {"err": None, "data": []})
        self.assertEqual(dispatcher.submission.result, JudgeStatus.SYSTEM_ERROR)
        update_problem.assert_not_called()

    def test_classroom_submission_queued_past_deadline_still_counts(self):
        now = timezone.now()
        contest = SimpleNamespace(id=9, start_time=now - timedelta(minutes=2),
                                  end_time=now - timedelta(seconds=1),
                                  status=ContestStatus.CONTEST_ENDED, fcu_quiz=object())
        dispatcher = self.make_dispatcher(contest)
        dispatcher.submission.create_time = now - timedelta(minutes=1)
        _, _, update_contest, update_rank = self.run_response(
            dispatcher, {"err": None, "data": [{"test_case": "1", "result": 0,
                                                  "cpu_time": 1, "memory": 1024}]})
        self.assertEqual(dispatcher.submission.result, JudgeStatus.ACCEPTED)
        update_contest.assert_called_once()
        update_rank.assert_called_once()

    def test_classroom_oi_rank_keeps_best_score(self):
        dispatcher = self.make_dispatcher(SimpleNamespace(id=9, fcu_quiz=object()))
        rank = SimpleNamespace(submission_info={"7": 8}, total_score=8, save=mock.Mock())
        dispatcher.submission.statistic_info = {"score": 3}
        dispatcher._update_oi_contest_rank(rank)
        self.assertEqual(rank.total_score, 8)
        rank.save.assert_not_called()
        dispatcher.submission.statistic_info = {"score": 10}
        dispatcher._update_oi_contest_rank(rank)
        self.assertEqual(rank.total_score, 10)
        self.assertEqual(rank.submission_info["7"], 10)
