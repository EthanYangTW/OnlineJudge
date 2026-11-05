"""Create the initial teacher administrator from environment variables only."""

import os

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from account.models import AdminType, ProblemPermission, User, UserProfile


class Command(BaseCommand):
    help = "Create the first FCU teacher administrator from FCU_TEACHER_USER/PASSWORD."

    def handle(self, *args, **options):
        with transaction.atomic():
            admins = User.objects.select_for_update().filter(admin_type=AdminType.SUPER_ADMIN)
            if admins.exists():
                if any(user.check_password("rootroot") for user in admins):
                    raise CommandError("An administrator still uses the upstream default password; reset it first")
                self.stdout.write("Administrator already exists")
                return

            username = os.environ.get("FCU_TEACHER_USER", "").strip()
            password = os.environ.get("FCU_TEACHER_PASSWORD", "")
            if not username or len(username) > 64 or len(password) < 12:
                raise CommandError("Set FCU_TEACHER_USER and FCU_TEACHER_PASSWORD (at least 12 characters)")
            if User.objects.filter(username__iexact=username).exists():
                raise CommandError("Teacher username belongs to an existing account")

            teacher = User(username=username, admin_type=AdminType.SUPER_ADMIN,
                           problem_permission=ProblemPermission.ALL)
            teacher.set_password(password)
            teacher.save()
            UserProfile.objects.create(user=teacher)
            self.stdout.write("Teacher administrator created")
