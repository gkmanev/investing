from django.contrib import admin

from .models import Investment


@admin.register(Investment)
class InvestmentAdmin(admin.ModelAdmin):
    list_display = ("name", "ticker", "category", "risk_level", "created_at")
    search_fields = ("name", "ticker", "category")
    list_filter = ("risk_level", "category")
