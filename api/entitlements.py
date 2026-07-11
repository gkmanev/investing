from __future__ import annotations

import os
from typing import Any

from .models import PremiumSubscription


DEFAULT_PLAN_ENTITLEMENTS = {
    "free": {
        "daily_queries": 10,
        "max_scan_limit": 5,
        "max_extra_pages": 0,
        "daily_analyze_stock": 1,
        "max_history_items": 8,
    },
    "pro": {
        "daily_queries": None,
        "max_scan_limit": None,
        "max_extra_pages": None,
        "daily_analyze_stock": None,
        "max_history_items": None,
    },
}


def _env_int(name: str, default: int | None) -> int | None:
    raw_value = os.getenv(name)
    if raw_value in (None, ""):
        return default
    return int(raw_value)


def get_plan_entitlements() -> dict[str, dict[str, Any]]:
    return {
        "free": {
            "daily_queries": _env_int(
                "PLAN_FREE_DAILY_QUERIES",
                DEFAULT_PLAN_ENTITLEMENTS["free"]["daily_queries"],
            ),
            "max_scan_limit": _env_int(
                "PLAN_FREE_MAX_SCAN_LIMIT",
                DEFAULT_PLAN_ENTITLEMENTS["free"]["max_scan_limit"],
            ),
            "max_extra_pages": _env_int(
                "PLAN_FREE_MAX_EXTRA_PAGES",
                DEFAULT_PLAN_ENTITLEMENTS["free"]["max_extra_pages"],
            ),
            "daily_analyze_stock": _env_int(
                "PLAN_FREE_DAILY_ANALYZE_STOCK",
                DEFAULT_PLAN_ENTITLEMENTS["free"]["daily_analyze_stock"],
            ),
            "max_history_items": _env_int(
                "PLAN_FREE_MAX_HISTORY_ITEMS",
                DEFAULT_PLAN_ENTITLEMENTS["free"]["max_history_items"],
            ),
        },
        "pro": {
            "daily_queries": _env_int(
                "PLAN_PRO_DAILY_QUERIES",
                DEFAULT_PLAN_ENTITLEMENTS["pro"]["daily_queries"],
            ),
            "max_scan_limit": _env_int(
                "PLAN_PRO_MAX_SCAN_LIMIT",
                DEFAULT_PLAN_ENTITLEMENTS["pro"]["max_scan_limit"],
            ),
            "max_extra_pages": _env_int(
                "PLAN_PRO_MAX_EXTRA_PAGES",
                DEFAULT_PLAN_ENTITLEMENTS["pro"]["max_extra_pages"],
            ),
            "daily_analyze_stock": _env_int(
                "PLAN_PRO_DAILY_ANALYZE_STOCK",
                DEFAULT_PLAN_ENTITLEMENTS["pro"]["daily_analyze_stock"],
            ),
            "max_history_items": _env_int(
                "PLAN_PRO_MAX_HISTORY_ITEMS",
                DEFAULT_PLAN_ENTITLEMENTS["pro"]["max_history_items"],
            ),
        },
    }


def _resolve_subscription(user) -> PremiumSubscription | None:
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    cached = getattr(user, "_premium_subscription_cache", None)
    if isinstance(cached, PremiumSubscription):
        return cached

    try:
        return user.premium_subscription
    except PremiumSubscription.DoesNotExist:
        return None


def resolve_effective_plan(user, subscription: PremiumSubscription | None = None) -> str:
    if user is None or not getattr(user, "is_authenticated", False):
        return "free"

    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return "pro"

    subscription = subscription if subscription is not None else _resolve_subscription(user)
    if subscription and subscription.is_active:
        return "pro"

    return "free"


def get_trial_days_left(user, *, plan: str | None = None) -> int | None:
    return None


def has_full_access(user, subscription: PremiumSubscription | None = None) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False))


def get_plan_context(
    user,
    subscription: PremiumSubscription | None = None,
) -> dict[str, Any]:
    plan = resolve_effective_plan(user, subscription=subscription)
    entitlements = dict(get_plan_entitlements()[plan])
    trial_days_left = get_trial_days_left(user, plan=plan)
    authenticated = bool(user is not None and getattr(user, "is_authenticated", False))

    return {
        "plan": plan,
        "trial_days_left": trial_days_left,
        "entitlements": entitlements,
        "has_full_access": authenticated,
        "trial_expired": False,
        "subscription_active": bool(subscription and subscription.is_active),
    }


def serialize_plan_context(
    user,
    subscription: PremiumSubscription | None = None,
) -> dict[str, Any]:
    plan_context = get_plan_context(user, subscription=subscription)
    return {
        "plan": plan_context["plan"],
        "trial_days_left": plan_context["trial_days_left"],
        "has_full_access": plan_context["has_full_access"],
        "entitlements": plan_context["entitlements"],
        "trial_expired": plan_context["trial_expired"],
    }
