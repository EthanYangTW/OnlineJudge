from django.apps import AppConfig


class FCUClassroomConfig(AppConfig):
    name = "fcu_classroom"

    def ready(self):
        from . import signals  # noqa: F401
