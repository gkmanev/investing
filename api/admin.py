from django.contrib import admin

from .models import Investment, ScreenerFilter, ScreenerType


@admin.register(Investment)
class InvestmentAdmin(admin.ModelAdmin):
    list_display = (
        "ticker",
        "category",
        "screener_type",
        "price",
        "market_cap",
        "options_suitability",
        "weekly_options",
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
