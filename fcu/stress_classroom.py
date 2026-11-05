#!/usr/bin/env python3
"""Exercise real HTTP C judging with isolated synthetic classroom accounts.

Run only against the loopback-published FCU Compose stack. The script creates
its own class, hidden cases, and students, then removes only that fixture.
"""

import argparse
import csv
import io
import json
import os
import secrets
import statistics
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener


CORRECT_C = ("#include <stdio.h>\nint main(void) { long long a, b; "
             'if (scanf("%lld %lld", &a, &b) != 2) return 1; '
             'printf("%lld\\n", a + b); return 0; }\n')
PARTIAL_C = '#include <stdio.h>\nint main(void) { puts("0"); return 0; }\n'
PENDING = {6, 7}


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("FCU_TEACHER_USER", "FCU_TEACHER_PASSWORD"):
        if not values.get(key) or values[key].startswith("REPLACE_WITH_"):
            raise RuntimeError("Teacher credentials are missing from the private env file")
    return values


class BrowserSession:
    def __init__(self, base_url, address=None):
        self.base_url = base_url.rstrip("/")
        self.address = address
        self.cookies = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookies))

    def raw(self, method, path, payload=None):
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            token = next((cookie.value for cookie in self.cookies if cookie.name == "csrftoken"), None)
            if token is None:
                self.raw("GET", "/api/profile")
                token = next((cookie.value for cookie in self.cookies if cookie.name == "csrftoken"), None)
            if token:
                headers["X-CSRFToken"] = token
        if self.address:
            # The loopback-only backend port is used for this synthetic IP test.
            # The production Caddy configuration must overwrite this header.
            headers["X-Real-IP"] = self.address
        request = Request(self.base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=20) as response:
                status, body = response.status, response.read()
        except HTTPError as error:
            status, body = error.code, error.read()
        try:
            result = json.loads(body)
        except ValueError as error:
            raise RuntimeError("Non-JSON response from {} {}: HTTP {}".format(method, path, status)) from error
        return status, result

    def call(self, method, path, payload=None):
        status, result = self.raw(method, path, payload)
        if status >= 400 or result.get("error") is not None:
            message = str(result.get("data", "request failed"))[:180]
            raise RuntimeError("{} {}: HTTP {}: {}".format(method, path, status, message))
        return result.get("data")


def compose_shell(compose_file, env_file, source):
    command = ["docker", "compose", "--env-file", str(env_file), "-f", str(compose_file),
               "exec", "-T", "oj-backend", "python", "manage.py", "shell", "-c",
               "exec(__import__('sys').stdin.read())"]
    result = subprocess.run(command, input=source, text=True, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError("Backend fixture command failed: " + result.stderr[-1000:])
    marker = "FCU_LOAD_RESULT="
    for line in result.stdout.splitlines():
        if line.startswith(marker):
            return json.loads(line[len(marker):])
    raise RuntimeError("Backend fixture command returned no result marker")


def provision_problem(compose_file, env_file, class_slug, run_id):
    fixture = {"class_slug": class_slug, "run_id": run_id}
    source = """
import hashlib, json, os, uuid
from pathlib import Path
from django.conf import settings
from fcu_classroom.models import Classroom, ClassroomQuiz
from problem.models import Problem, ProblemRuleType

fixture = json.loads(FIXTURE_JSON)
classroom = Classroom.objects.get(slug=fixture['class_slug'], name='FCU load test ' + fixture['run_id'])
quiz = ClassroomQuiz.objects.get(classroom=classroom, slug='compiler')
case_id = uuid.uuid4().hex
case_dir = Path(settings.TEST_CASE_DIR) / case_id
case_dir.mkdir(mode=0o710)
os.chown(case_dir, 900, 903)
cases = [(b'1 2\\n', b'3\\n'), (b'-5 8\\n', b'3\\n'), (b'0 0\\n', b'0\\n')]
info = {'spj': False, 'test_cases': {}}
scores = []
for number, (input_bytes, output_bytes) in enumerate(cases, 1):
    input_name, output_name = f'{number}.in', f'{number}.out'
    (case_dir / input_name).write_bytes(input_bytes)
    (case_dir / output_name).write_bytes(output_bytes)
    item = {'stripped_output_md5': hashlib.md5(output_bytes.rstrip()).hexdigest(),
            'input_size': len(input_bytes), 'output_size': len(output_bytes),
            'input_name': input_name, 'output_name': output_name}
    info['test_cases'][str(number)] = item
    scores.append({'input_name': input_name, 'output_name': output_name, 'score': 10})
(case_dir / 'info').write_text(json.dumps(info), encoding='utf-8')
for path in case_dir.iterdir():
    os.chown(path, 900, 903)
    path.chmod(0o640)
problem = Problem.objects.create(
    _id='A', contest=quiz.contest, title='Add two integers',
    description='Read two signed integers and print their sum.',
    input_description='Two signed integers, separated by a space.',
    output_description='Their sum and a newline.',
    samples=[{'input': '1 2\\n', 'output': '3\\n'}],
    test_case_id=case_id, test_case_score=scores, languages=['C'], template={},
    created_by=classroom.teacher, time_limit=1000, memory_limit=128,
    rule_type=ProblemRuleType.OI, difficulty='Low', total_score=30,
    visible=True, share_submission=False)
print('FCU_LOAD_RESULT=' + json.dumps({'problemId': problem.id}))
"""
    return compose_shell(compose_file, env_file,
                         "FIXTURE_JSON = {!r}\n".format(json.dumps(fixture)) + source)


def cleanup_fixture(compose_file, env_file, class_slug, run_id, usernames):
    fixture = {"class_slug": class_slug, "run_id": run_id, "usernames": usernames}
    source = """
import json, shutil
from pathlib import Path
from django.conf import settings
from account.models import User
from contest.models import Contest
from fcu_classroom.models import Classroom
from problem.models import Problem

fixture = json.loads(FIXTURE_JSON)
classroom = Classroom.objects.filter(slug=fixture['class_slug'],
                                     name='FCU load test ' + fixture['run_id']).first()
if classroom is None or not fixture['class_slug'].startswith('fcu-load-'):
    print('FCU_LOAD_RESULT=' + json.dumps({'removed': False}))
else:
    contest_ids = list(classroom.quizzes.values_list('contest_id', flat=True))
    case_ids = list(Problem.objects.filter(contest_id__in=contest_ids).values_list('test_case_id', flat=True))
    classroom.delete()
    Contest.objects.filter(id__in=contest_ids).delete()
    names = [name for name in fixture['usernames'] if name.startswith('load' + fixture['run_id'])]
    User.objects.filter(username__in=names).delete()
    for case_id in case_ids:
        if len(case_id) == 32 and all(char in '0123456789abcdef' for char in case_id):
            shutil.rmtree(Path(settings.TEST_CASE_DIR) / case_id, ignore_errors=True)
    print('FCU_LOAD_RESULT=' + json.dumps({'removed': True, 'students': len(names)}))
"""
    return compose_shell(compose_file, env_file,
                         "FIXTURE_JSON = {!r}\n".format(json.dumps(fixture)) + source)


def save_private_fixture(class_slug, run_id, usernames, password, quiz=None):
    path = Path(tempfile.gettempdir()) / (class_slug + ".json")
    payload = {"classSlug": class_slug, "runId": run_id, "logins": usernames,
               "login": usernames[0], "password": password,
               "quizId": quiz["id"] if quiz else None}
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream)
        stream.write("\n")
    return path


def percentile(values, fraction):
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def student_run(base_url, class_slug, quiz, question, username, password, ordinal, deadline):
    address = "192.0.2.{}".format(ordinal + 10)
    session = BrowserSession(base_url, address)
    root = "/api/fcu/classroom/classes/{}/".format(class_slug)
    session.call("POST", "/api/fcu/classroom/login/",
                 {"class": class_slug, "login": username, "password": password})
    state = session.call("GET", root + "state/")
    if not any(item["id"] == quiz["id"] for item in state["quizzes"]):
        raise RuntimeError("Published quiz absent for " + username)
    code = CORRECT_C if ordinal % 2 == 0 else PARTIAL_C
    draft_path = "quizzes/{}/questions/{}/draft/".format(quiz["id"], question["id"])
    session.call("POST", root + draft_path, {"code": code})
    if session.call("GET", root + draft_path)["code"] != code:
        raise RuntimeError("Draft was not persisted for " + username)
    started = time.monotonic()
    submission = session.call("POST", "/api/submission",
                              {"contest_id": quiz["contestId"], "problem_id": question["problemId"],
                               "language": "C", "code": code})
    submission_id = submission["submission_id"]
    submit_latency = time.monotonic() - started
    while time.monotonic() < deadline:
        result = session.call("GET", "/api/submission?" + urlencode({"id": submission_id}))
        if result["result"] not in PENDING:
            break
        time.sleep(0.7)
    else:
        raise RuntimeError("Timed out waiting for C judge on " + username)
    elapsed = time.monotonic() - started
    expected = 30 if ordinal % 2 == 0 else 10
    raw_score = result.get("statistic_info", {}).get("score")
    if result["result"] == 5 or raw_score != expected or result.get("code") != code:
        raise RuntimeError("Wrong judge result or saved code for {}: verdict={}, score={}, "
                           "code_match={}, code_length={}".format(
                               username, result["result"], raw_score, result.get("code") == code,
                               len(result.get("code") or "")))
    state = session.call("GET", root + "state/")
    actual = state["scores"].get(str(question["id"]), {}).get("best")
    if actual != expected:
        raise RuntimeError("Score was not persisted for {}: {}".format(username, actual))
    history = session.call("GET", root + "quizzes/{}/submissions/".format(quiz["id"]))
    if not any(item.get("submissionId") == submission_id and item.get("code") == code for item in history):
        raise RuntimeError("Saved code missing from history for " + username)
    status, _ = session.raw("GET", root + "scores/")
    if status != 403:
        raise RuntimeError("Student could access teacher scores: HTTP " + str(status))
    return {"username": username, "score": expected, "submit_seconds": submit_latency,
            "judge_seconds": elapsed, "submission_id": submission_id, "session": session}


def run(args):
    parsed = urlparse(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost"):
        raise RuntimeError("Use the Compose stack's loopback HTTP port, never the live student URL")
    env = read_env(args.env_file)
    if env.get("FCU_BIND", "127.0.0.1") != "127.0.0.1" or parsed.port != int(env.get("FCU_HTTP_PORT", "5482")):
        raise RuntimeError("The load test requires the Compose loopback bind and configured HTTP port")
    run_id = secrets.token_hex(4)
    class_slug = "fcu-load-" + run_id
    usernames = ["load{}{:02d}".format(run_id, number) for number in range(args.students)]
    password = secrets.token_urlsafe(24)
    teacher = BrowserSession(args.base_url)
    fixture_created = False
    try:
        teacher.call("POST", "/api/login",
                     {"username": env["FCU_TEACHER_USER"], "password": env["FCU_TEACHER_PASSWORD"]})
        base = "/api/fcu/classroom/classes/"
        teacher.call("POST", base, {"slug": class_slug, "name": "FCU load test " + run_id})
        fixture_created = True
        class_base = base + class_slug + "/"
        rows = io.StringIO()
        writer = csv.writer(rows)
        writer.writerow(["login", "name", "class", "password"])
        for number, username in enumerate(usernames, 1):
            writer.writerow([username, "Synthetic student {:02d}".format(number), "LOAD", password])
        teacher.call("POST", class_base + "roster/", {"csv": rows.getvalue()})
        now = datetime.now(timezone.utc)
        quiz = teacher.call("POST", class_base + "quizzes/",
                            {"slug": "compiler", "title": "C compiler load test", "mode": "exam",
                             "published": True, "startsAt": (now - timedelta(minutes=1)).isoformat(),
                             "endsAt": (now + timedelta(minutes=30)).isoformat()})
        problem = provision_problem(args.compose_file, args.env_file, class_slug, run_id)
        question = teacher.call("POST", class_base + "quizzes/{}/questions/".format(quiz["id"]),
                                {"slug": "add-two", "title": "Add two integers", "kind": "c_lab",
                                 "points": 30, "prompt": "Read two signed integers and print their sum.",
                                 "problemId": problem["problemId"]})
        print("Created isolated {}-student fixture {}".format(args.students, class_slug), flush=True)
        deadline = time.monotonic() + args.timeout
        results = []
        with ThreadPoolExecutor(max_workers=min(args.students, 32)) as pool:
            futures = [pool.submit(student_run, args.base_url, class_slug, quiz, question,
                                   username, password, number, deadline)
                       for number, username in enumerate(usernames)]
            for future in as_completed(futures):
                results.append(future.result())
        results.sort(key=lambda item: item["username"])
        teacher_scores = teacher.call("GET", class_base + "scores/")
        by_name = {row["login"]: row for row in teacher_scores}
        for result in results:
            score = by_name[result["username"]]["scores"].get(str(question["id"]), {}).get("best")
            if score != result["score"]:
                raise RuntimeError("Teacher score mismatch for " + result["username"])
        if len(results) > 1:
            first, second = results[0], results[1]
            peer_path = "/api/submission?" + urlencode({"id": first["submission_id"]})
            status, payload = second["session"].raw("GET", peer_path)
            if status < 400 and payload.get("error") is None:
                raise RuntimeError("Cross-student submission was visible")
        latencies = [item["judge_seconds"] for item in results]
        submits = [item["submit_seconds"] for item in results]
        print("C results: {} full score, {} partial score, {} system errors".format(
            sum(item["score"] == 30 for item in results), sum(item["score"] == 10 for item in results), 0))
        print("Submission HTTP latency: median {:.2f}s, p95 {:.2f}s, max {:.2f}s".format(
            statistics.median(submits), percentile(submits, 0.95), max(submits)))
        print("Submission-to-grade latency: median {:.2f}s, p95 {:.2f}s, max {:.2f}s".format(
            statistics.median(latencies), percentile(latencies, 0.95), max(latencies)))
        print("Verified drafts, saved C source, student scores, teacher scores, and peer isolation")
    finally:
        if fixture_created and not args.keep:
            result = cleanup_fixture(args.compose_file, args.env_file, class_slug, run_id, usernames)
            print("Removed isolated fixture: {}".format(result.get("removed", False)))
        elif fixture_created:
            teacher.call("POST", "/api/fcu/classroom/classes/{}/ip-lock/".format(class_slug),
                         {"action": "set-global", "enabled": False})
            path = save_private_fixture(class_slug, run_id, usernames, password, locals().get("quiz"))
            print("Kept isolated fixture {} with its IP lock disabled for UI review. "
                  "Private test login file: {}".format(class_slug, path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(__file__).with_name(".env"))
    parser.add_argument("--compose-file", type=Path, default=Path(__file__).with_name("compose.yaml"))
    parser.add_argument("--base-url", default="http://127.0.0.1:5482")
    parser.add_argument("--students", type=int, default=22)
    parser.add_argument("--timeout", type=int, default=240, help="overall judging deadline in seconds")
    parser.add_argument("--keep", action="store_true", help="retain fixture and write a mode-0600 test login file")
    parser.add_argument("--cleanup-from", type=Path, help="remove a fixture recorded by --keep")
    args = parser.parse_args()
    if not 1 <= args.students <= 100:
        parser.error("--students must be between 1 and 100")
    if args.timeout < 30:
        parser.error("--timeout must be at least 30 seconds")
    try:
        if args.cleanup_from:
            fixture = json.loads(args.cleanup_from.read_text(encoding="utf-8"))
            result = cleanup_fixture(args.compose_file, args.env_file, fixture["classSlug"],
                                     fixture["runId"], fixture["logins"])
            if not result.get("removed"):
                raise RuntimeError("Fixture marker did not match; no data removed")
            args.cleanup_from.unlink()
            print("Removed isolated fixture " + fixture["classSlug"])
        else:
            run(args)
    except (OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
        print("LOAD TEST FAILED: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
