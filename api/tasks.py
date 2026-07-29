import logging

from celery import shared_task
from django.core.management import call_command
from django.utils import timezone

from .models import AgentRun


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


@shared_task(ignore_result=True, name="api.tasks.run_populate_daily_brief")
def run_populate_daily_brief() -> None:
    logger.info("Starting populate_daily_brief task.")
    call_command("populate_daily_brief")
    logger.info("Finished populate_daily_brief task.")


@shared_task(ignore_result=True, name="api.tasks.run_trading_view_scrape")
def run_trading_view_scrape(skip_rsi: bool = False, **kwargs: object) -> None:
    if "skip_rsi" in kwargs:
        skip_rsi = bool(kwargs["skip_rsi"])
    logger.info("Starting trading_view_scrape task (skip_rsi=%s).", skip_rsi)
    call_command("trading_view_scrape", skip_rsi=skip_rsi)
    logger.info("Finished trading_view_scrape task (skip_rsi=%s).", skip_rsi)


@shared_task(ignore_result=True, name="api.tasks.run_initial_screener")
def run_initial_screener() -> None:
    logger.info("Starting initial_screener task.")
    call_command("initial_screener")
    logger.info("Finished initial_screener task.")


@shared_task(ignore_result=True, name="api.tasks.run_agent_run")
def run_agent_run(agent_run_id: int) -> None:
    try:
        agent_run = AgentRun.objects.get(pk=agent_run_id)
    except AgentRun.DoesNotExist:
        logger.error("AgentRun %s does not exist.", agent_run_id)
        return

    logger.info("AgentRun %s starting.", agent_run_id)
    agent_run.status = AgentRun.Status.RUNNING
    agent_run.started_at = timezone.now()
    agent_run.finished_at = None
    agent_run.error_text = ""
    agent_run.result_text = ""
    agent_run.result_blocks_json = []
    agent_run.used_tools_json = []
    agent_run.llm_usage_json = []
    agent_run.llm_usage_summary_json = {}
    agent_run.save(
        update_fields=[
            "status",
            "started_at",
            "finished_at",
            "error_text",
            "result_text",
            "result_blocks_json",
            "used_tools_json",
            "llm_usage_json",
            "llm_usage_summary_json",
            "updated_at",
        ]
    )

    from .agent_views import run_agent
    from .entitlements import get_plan_context

    try:
        result = run_agent(
            agent_run.query,
            agent_run.history_json,
            agent_run_id=agent_run_id,
            user=agent_run.user,
            plan_context=get_plan_context(agent_run.user),
        )
    except Exception as exc:
        agent_run.status = AgentRun.Status.FAILED
        agent_run.error_text = str(exc)
        agent_run.result_text = ""
        agent_run.result_blocks_json = []
        agent_run.used_tools_json = []
        agent_run.finished_at = timezone.now()
        agent_run.save(
            update_fields=[
                "status",
                "error_text",
                "result_text",
                "result_blocks_json",
                "used_tools_json",
                "finished_at",
                "updated_at",
            ]
        )
        logger.exception("AgentRun %s failed.", agent_run_id)
        raise

    agent_run.status = AgentRun.Status.COMPLETED
    agent_run.result_text = result.get("answer") or ""
    agent_run.history_json = result.get("history") or []
    agent_run.result_blocks_json = result.get("blocks") or []
    agent_run.used_tools_json = result.get("used_tools") or []
    agent_run.llm_usage_json = result.get("llm_usage") or []
    agent_run.llm_usage_summary_json = result.get("llm_usage_summary") or {}
    agent_run.finished_at = timezone.now()
    agent_run.save(
        update_fields=[
            "status",
            "result_text",
            "history_json",
            "result_blocks_json",
            "used_tools_json",
            "llm_usage_json",
            "llm_usage_summary_json",
            "finished_at",
            "updated_at",
        ]
    )
    if agent_run.conversation_id:
        conversation = agent_run.conversation
        conversation.preview = agent_run.result_text[:500]
        conversation.save(update_fields=["preview", "updated_at"])
    logger.info("AgentRun %s completed.", agent_run_id)
