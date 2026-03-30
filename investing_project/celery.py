import os

import django
from celery import Celery


os.environ.setdefault("DJANGO_SETTINGS_MODULE", "investing_project.settings")

app = Celery("investing_project")

django.setup()

app.config_from_object("django.conf:settings", namespace="CELERY")
app.conf.broker_connection_retry_on_startup = True
app.autodiscover_tasks()

import api.tasks  # noqa: E402 — explicit import ensures tasks are registered


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
