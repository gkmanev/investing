import logging

from celery import shared_task
from django.core.management import call_command

from .daily_brief_services import send_daily_brief_to_active_subscribers


logger = logging.getLogger(__name__)


@shared_task(ignore_result=True)
def send_daily_top_3_edition() -> None:
    result = send_daily_brief_to_active_subscribers()
    logger.info(
        "Daily Top 3 task finished for %s already_sent=%s recipients=%s",
        result["edition_date"],
        result["already_sent"],
        result["recipient_count"],
    )


@shared_task(ignore_result=True)
def run_trading_view_scrape() -> None:
    logger.info("Starting trading_view_scrape task.")
    call_command("trading_view_scrape")
    logger.info("Finished trading_view_scrape task.")


@shared_task(ignore_result=True)
def run_initial_screener() -> None:
    logger.info("Starting initial_screener task.")
    call_command("initial_screener")
    logger.info("Finished initial_screener task.")
