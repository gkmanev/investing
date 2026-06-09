from django.contrib import admin

from .models import (
    AgentRun,
    DailyBriefEdition,
    DailyBriefSubscription,
    EmailVerificationToken,
    Investment,
    ScreenerFilter,
    ScreenerType,
)


@admin.register(AgentRun)
class AgentRunAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "user",
        "status",
        "started_at",
        "finished_at",
        "created_at",
    )
    list_filter = ("status", "started_at", "finished_at", "created_at")
    search_fields = ("user__username", "user__email", "query", "result_text", "error_text")
    readonly_fields = (
        "user",
        "query",
        "history_json",
        "status",
        "result_text",
        "error_text",
        "started_at",
        "finished_at",
        "created_at",
        "updated_at",
    )


@admin.register(Investment)
class InvestmentAdmin(admin.ModelAdmin):
    list_display = (
        "ticker",
        "category",
        "screener_type",
        "price",
        "market_cap",
        "option_exp",
        "created_at",
    )
    search_fields = ("ticker", "category", "screener_type")
    list_filter = ("category", "screener_type")

    class Media:
        css = {"all": ("css/price_badges.css",)}


@admin.register(ScreenerType)
class ScreenerTypeAdmin(admin.ModelAdmin):
    list_display = ("name", "created_at", "updated_at")
    search_fields = ("name",)


@admin.register(ScreenerFilter)
class ScreenerFilterAdmin(admin.ModelAdmin):
    list_display = ("label", "screener_type", "display_order", "created_at")
    list_filter = ("screener_type",)
    search_fields = ("label", "screener_type__name")


@admin.register(EmailVerificationToken)
class EmailVerificationTokenAdmin(admin.ModelAdmin):
    list_display = ("user", "token", "expires_at", "created_at")
    search_fields = ("user__username", "user__email")


@admin.register(DailyBriefSubscription)
class DailyBriefSubscriptionAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "status",
        "is_active",
        "source",
        "subscribed_at",
        "unsubscribed_at",
    )
    list_filter = ("status", "source", "is_active", "subscribed_at")
    search_fields = ("user__username", "user__email")


@admin.register(DailyBriefEdition)
class DailyBriefEditionAdmin(admin.ModelAdmin):
    list_display = ("edition_date", "recipient_count", "sent_at", "created_at")
    list_filter = ("edition_date", "sent_at")
    readonly_fields = (
        "edition_date",
        "subject",
        "body_text",
        "top_symbols",
        "recipient_count",
        "sent_at",
        "created_at",
        "updated_at",
    )
