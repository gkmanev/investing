import logging

from celery import shared_task

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
