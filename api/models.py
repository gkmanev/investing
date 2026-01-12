from django.db import models


class ScreenerType(models.Model):
    """Represents a type of screener exposed by the upstream API."""

    name = models.CharField(max_length=255, unique=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return self.name


class ScreenerFilter(models.Model):
    """Filter associated with a screener type."""

    screener_type = models.ForeignKey(
        ScreenerType, related_name="filters", on_delete=models.CASCADE
    )
    label = models.CharField(max_length=255)
    payload = models.JSONField(blank=True, null=True)
    display_order = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["display_order", "id"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"{self.screener_type.name}: {self.label}"


class Investment(models.Model):
    """Represents an investment option exposed through the API."""

    ticker = models.CharField(max_length=50, unique=True)
    category = models.CharField(max_length=100)
    screener_type = models.CharField(max_length=255, blank=True, null=True)
    price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    roi = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    rsi = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    volume = models.BigIntegerField(null=True, blank=True)
    market_cap = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    options_suitability = models.SmallIntegerField(null=True, blank=True)
    option_exp = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ticker"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return self.ticker


class FinancialStatement(models.Model):
    """Stores a raw financial statement payload for a symbol."""

    symbol = models.CharField(max_length=25)
    target_currency = models.CharField(max_length=10, default="USD")
    period_type = models.CharField(max_length=20, default="annual")
    statement_type = models.CharField(max_length=50, default="income-statement")
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["symbol", "period_type", "statement_type", "target_currency"]
        unique_together = (
            "symbol",
            "period_type",
            "statement_type",
            "target_currency",
        )

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return (
            f"{self.symbol} {self.statement_type} ({self.period_type}, "
            f"{self.target_currency})"
        )


class DueDiligenceReport(models.Model):
    """Stores structured AI due diligence reports for a symbol."""

    symbol = models.CharField(max_length=16, db_index=True)
    rating = models.CharField(max_length=16, db_index=True)
    confidence = models.FloatField(null=True, blank=True)
    model_name = models.CharField(max_length=64, blank=True, default="")
    report = models.JSONField()
    financial_data = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "symbol"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"{self.symbol} {self.rating} ({self.created_at:%Y-%m-%d})"
