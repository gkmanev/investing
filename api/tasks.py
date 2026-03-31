import logging

from celery import shared_task
from django.core.management import call_command


logger = logging.getLogger(__name__)


@shared_task(ignore_result=True, name="api.tasks.send_daily_top_3_edition")
def send_daily_top_3_edition() -> None:
    from .daily_brief_services import send_daily_brief_to_active_subscribers

    result = send_daily_brief_to_active_subscribers()
    logger.info(
        "Daily Top 3 task finished for %s already_sent=%s recipients=%s",
        result["edition_date"],
        result["already_sent"],
        result["recipient_count"],
    )


@shared_task(ignore_result=True, name="api.tasks.run_trading_view_scrape")
def run_trading_view_scrape(*, skip_rsi: bool = False) -> None:
    logger.info("Starting trading_view_scrape task (skip_rsi=%s).", skip_rsi)
    call_command("trading_view_scrape", skip_rsi=skip_rsi)
    logger.info("Finished trading_view_scrape task (skip_rsi=%s).", skip_rsi)


@shared_task(ignore_result=True, name="api.tasks.run_initial_screener")
def run_initial_screener() -> None:
    logger.info("Starting initial_screener task.")
    call_command("initial_screener")
    logger.info("Finished initial_screener task.")
