from django.core.management import call_command
from django.test import TestCase, override_settings
from django_celery_beat.models import CrontabSchedule, PeriodicTask


@override_settings(CELERY_TIMEZONE="UTC")
class TradingJobsScheduleCommandTestCase(TestCase):
    def test_sync_trading_jobs_schedule_creates_expected_periodic_tasks(self) -> None:
        call_command("sync_trading_jobs_schedule")

        expected_trading_tasks = {
            ("trading-view-scrape-1300-utc", "13", "0"),
            ("trading-view-scrape-1345-utc", "13", "45"),
            ("trading-view-scrape-1430-utc", "14", "30"),
            ("trading-view-scrape-1515-utc", "15", "15"),
            ("trading-view-scrape-1600-utc", "16", "0"),
            ("trading-view-scrape-1645-utc", "16", "45"),
            ("trading-view-scrape-1730-utc", "17", "30"),
            ("trading-view-scrape-1815-utc", "18", "15"),
            ("trading-view-scrape-1900-utc", "19", "0"),
            ("trading-view-scrape-1945-utc", "19", "45"),
        }

        for task_name, hour, minute in expected_trading_tasks:
            task = PeriodicTask.objects.get(name=task_name)
            self.assertEqual(task.task, "api.tasks.run_trading_view_scrape")
            self.assertTrue(task.enabled)
            self.assertEqual(task.crontab.hour, hour)
            self.assertEqual(task.crontab.minute, minute)
            self.assertEqual(str(task.crontab.timezone), "UTC")

        initial_screener_task = PeriodicTask.objects.get(name="initial-screener-daily")
        self.assertEqual(initial_screener_task.task, "api.tasks.run_initial_screener")
        self.assertTrue(initial_screener_task.enabled)
        self.assertEqual(initial_screener_task.crontab.hour, "12")
        self.assertEqual(initial_screener_task.crontab.minute, "30")
        self.assertEqual(str(initial_screener_task.crontab.timezone), "UTC")

    def test_sync_trading_jobs_schedule_updates_existing_tasks_and_removes_stale_ones(self) -> None:
        stale_schedule = CrontabSchedule.objects.create(
            minute="5",
            hour="11",
            day_of_week="*",
            day_of_month="*",
            month_of_year="*",
            timezone="UTC",
        )
        PeriodicTask.objects.create(
            name="trading-view-scrape-1300-utc",
            task="api.tasks.run_trading_view_scrape",
            crontab=stale_schedule,
            enabled=False,
        )
        PeriodicTask.objects.create(
            name="trading-view-scrape-2045-utc",
            task="api.tasks.run_trading_view_scrape",
            crontab=stale_schedule,
            enabled=True,
        )
        PeriodicTask.objects.create(
            name="initial-screener-daily",
            task="api.tasks.run_initial_screener",
            crontab=stale_schedule,
            enabled=False,
        )

        call_command("sync_trading_jobs_schedule")

        updated_trading_task = PeriodicTask.objects.get(name="trading-view-scrape-1300-utc")
        self.assertTrue(updated_trading_task.enabled)
        self.assertEqual(updated_trading_task.crontab.hour, "13")
        self.assertEqual(updated_trading_task.crontab.minute, "0")
        self.assertFalse(
            PeriodicTask.objects.filter(name="trading-view-scrape-2045-utc").exists()
        )

        updated_initial_screener = PeriodicTask.objects.get(name="initial-screener-daily")
        self.assertTrue(updated_initial_screener.enabled)
        self.assertEqual(updated_initial_screener.crontab.hour, "12")
        self.assertEqual(updated_initial_screener.crontab.minute, "30")
