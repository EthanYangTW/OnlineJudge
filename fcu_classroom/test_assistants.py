import json
import os
from unittest.mock import patch

from django.test import Client, TestCase

from account.models import AdminType, User, UserProfile
from contest.models import Contest

from .models import Classroom, ClassroomAssistant, Enrollment


class AssistantAccessTest(TestCase):
    def setUp(self):
        self.teacher = User.objects.create(username="owner-a", admin_type=AdminType.ADMIN)
        self.other_teacher = User.objects.create(username="owner-b", admin_type=AdminType.ADMIN)
        self.owner_client = Client()
        self.owner_client.force_login(self.teacher)
        self.other_client = Client()
        self.other_client.force_login(self.other_teacher)
        self.first = Classroom.objects.create(slug="class-a", name="Class A", teacher=self.teacher)
        self.second = Classroom.objects.create(slug="class-b", name="Class B", teacher=self.other_teacher)
        self.same_owner = Classroom.objects.create(slug="class-c", name="Class C", teacher=self.teacher)
        self.base = "/api/fcu/classroom/classes/"

    def post(self, client, path, data):
        return client.post(self.base + path, data=json.dumps(data), content_type="application/json",
                           REMOTE_ADDR="192.0.2.3")

    def test_owner_creates_class_scoped_assistant_without_global_admin(self):
        password = "unique-long-TA-passphrase"
        created = self.post(self.owner_client, "class-a/assistants/", {
            "login": "ta-a", "name": "Teaching Assistant", "password": password})
        self.assertEqual(created.status_code, 201, created.content)
        self.assertNotIn(password, created.content.decode("utf-8"))
        self.assertEqual(created.json()["data"]["name"], "Teaching Assistant")
        user = User.objects.get(username="ta-a")
        self.assertEqual(user.admin_type, AdminType.REGULAR_USER)
        self.assertTrue(user.check_password(password))
        self.assertNotEqual(user.password, password)
        self.assertEqual(UserProfile.objects.get(user=user).real_name, "Teaching Assistant")

        with patch.dict(os.environ, {"FCU_CLASSROOM_ONLY": "true"}):
            assistant = Client()
            logged_in = assistant.post("/api/login", data=json.dumps({
                "username": "ta-a", "password": password}), content_type="application/json",
                REMOTE_ADDR="192.0.2.3")
            self.assertIsNone(logged_in.json()["error"])
            classes = assistant.get(self.base, REMOTE_ADDR="192.0.2.3")
            self.assertEqual([item["slug"] for item in classes.json()["data"]], ["class-a"])
            self.assertEqual(classes.json()["data"][0]["role"], "assistant")
            state = assistant.get(self.base + "class-a/state/", REMOTE_ADDR="192.0.2.3")
            self.assertTrue(state.json()["data"]["actor"]["teacher"])
            self.assertEqual(state.json()["data"]["classroom"]["role"], "assistant")
            self.assertEqual(assistant.get(self.base + "class-b/state/",
                                           REMOTE_ADDR="192.0.2.3").status_code, 404)
            quiz = self.post(assistant, "class-a/quizzes/", {
                "slug": "ta-practice", "title": "TA practice", "mode": "practice",
                "published": False})
            self.assertEqual(quiz.status_code, 201, quiz.content)
            self.assertEqual(Contest.objects.get(id=quiz.json()["data"]["contestId"]).created_by_id,
                             self.teacher.id)
            self.assertEqual(self.post(assistant, "class-b/quizzes/", {
                "slug": "foreign", "title": "Foreign", "mode": "practice"}).status_code, 403)
            self.assertEqual(self.post(assistant, "class-a/assistants/", {
                "login": "ta-b", "name": "Another", "password": password}).status_code, 403)
            self.assertEqual(self.post(assistant, "", {"slug": "new-class", "name": "New"}).status_code, 403)
            self.assertIsNotNone(assistant.get("/api/admin/user",
                                               REMOTE_ADDR="192.0.2.3").data["error"])

    def test_reuse_is_limited_to_classes_of_same_owner_and_removal_revokes_class_access(self):
        self.post(self.owner_client, "class-a/assistants/", {
            "login": "ta-shared", "name": "TA Shared", "password": "another-long-TA-password"})
        foreign = self.post(self.other_client, "class-b/assistants/", {
            "login": "ta-shared", "existing": True})
        self.assertEqual(foreign.status_code, 400)
        added = self.post(self.owner_client, "class-c/assistants/", {
            "login": "ta-shared", "existing": True})
        self.assertEqual(added.status_code, 201)
        self.assertEqual(ClassroomAssistant.objects.filter(user__username="ta-shared").count(), 2)
        assistant = Client()
        assistant.force_login(User.objects.get(username="ta-shared"))
        removed = self.owner_client.delete(self.base + "class-a/assistants/ta-shared/",
                                           REMOTE_ADDR="192.0.2.3")
        self.assertEqual(removed.status_code, 200)
        self.assertEqual(assistant.get(self.base + "class-a/state/",
                                       REMOTE_ADDR="192.0.2.3").status_code, 404)
        self.assertEqual(assistant.get(self.base + "class-c/state/",
                                       REMOTE_ADDR="192.0.2.3").status_code, 200)

    def test_existing_student_and_weak_password_cannot_be_assigned(self):
        student = User.objects.create(username="student-a", admin_type=AdminType.REGULAR_USER)
        Enrollment.objects.create(classroom=self.first, user=student)
        take_over = self.post(self.owner_client, "class-a/assistants/", {
            "login": "student-a", "name": "Student", "password": "unique-long-password"})
        self.assertEqual(take_over.status_code, 409)
        self.assertFalse(ClassroomAssistant.objects.filter(user=student).exists())
        weak = self.post(self.owner_client, "class-a/assistants/", {
            "login": "ta-weak", "name": "Weak", "password": "ta-weak"})
        self.assertEqual(weak.status_code, 400)
        self.assertFalse(User.objects.filter(username="ta-weak").exists())
