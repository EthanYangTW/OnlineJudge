"""Create one classroom and import its private roster without printing passwords."""

import json
import os
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.test import RequestFactory

from account.models import User
from fcu_classroom.models import Classroom
from fcu_classroom.views import ClassroomRosterView, SLUG_RE


class Command(BaseCommand):
    help = "Create an FCU classroom and import a UTF-8 roster CSV from a private file."

    def add_arguments(self, parser):
        parser.add_argument("--class-slug", required=True)
        parser.add_argument("--class-name", required=True)
        parser.add_argument("--roster", required=True)
        parser.add_argument("--teacher", default=os.environ.get("FCU_TEACHER_USER", "teacher"))

    def handle(self, *args, **options):
        slug = options["class_slug"]
        name = options["class_name"].strip()
        if not SLUG_RE.fullmatch(slug) or not 1 <= len(name) <= 160:
            raise CommandError("Invalid classroom ID or name")
        teacher = User.objects.filter(username=options["teacher"]).first()
        if teacher is None or not teacher.is_admin_role():
            raise CommandError("Teacher admin account does not exist")
        try:
            raw = Path(options["roster"]).read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise CommandError("Cannot read private roster CSV") from exc
        if len(raw.encode("utf-8")) > 262144:
            raise CommandError("Roster CSV exceeds 256 KiB")

        with transaction.atomic():
            classroom, created = Classroom.objects.get_or_create(
                slug=slug, defaults={"name": name, "teacher": teacher})
            if not created and (classroom.teacher_id != teacher.id or classroom.name != name):
                raise CommandError("Classroom exists with a different owner or name")
            request = RequestFactory().post(
                "/api/fcu/classroom/classes/{}/roster/".format(slug),
                data=json.dumps({"csv": raw}), content_type="application/json",
                REMOTE_ADDR="127.0.0.1")
            request.user = teacher
            response = ClassroomRosterView.as_view()(request, slug=slug)
            result = json.loads(response.content)
            if response.status_code != 200 or result.get("error"):
                raise CommandError(str(result.get("data") or "Roster import failed"))
        self.stdout.write("Provisioned {} with {} students".format(slug, result["data"]["imported"]))
