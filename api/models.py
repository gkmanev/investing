import uuid

from django.conf import settings
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
    label = models.TextField()
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

    ticker = models.CharField(max_length=50)
    option_symbol = models.CharField(max_length=50, unique=True, null=True, blank=True)
    category = models.CharField(max_length=100)
    screener_type = models.CharField(max_length=255, blank=True, null=True)
    price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    strike_price = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True
    )
    bid = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    ask = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    mid = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    option_data = models.JSONField(null=True, blank=True)
    rsi = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    volume = models.BigIntegerField(null=True, blank=True)
    market_cap = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    option_exp = models.DateField(null=True, blank=True)
    next_earnings_date = models.DateField(null=True, blank=True)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ticker"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return self.option_symbol or self.ticker


class CboeSecurity(models.Model):
    """Tracks tickers with weekly options availability from CBOE."""

    symbol = models.CharField(max_length=25, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["symbol"]
        db_table = "cboe_securities"

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return self.symbol


class Symbol(models.Model):
    """Master list of exchange-listed symbols with market cap >= 5B."""

    ticker = models.CharField(max_length=50, unique=True)
    exchange = models.CharField(max_length=100, blank=True, default="")
    market_cap = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    LIQUIDITY_GOOD = "GOOD"
    LIQUIDITY_WEAK = "WEAK"
    LIQUIDITY_CHOICES = [
        (LIQUIDITY_GOOD, "Good"),
        (LIQUIDITY_WEAK, "Weak"),
    ]

    initial_suitability = models.BooleanField(null=True, blank=True, default=None)
    score = models.IntegerField(null=True, blank=True)
    classification = models.CharField(max_length=100, blank=True, null=True)
    liquidity = models.CharField(
        max_length=10,
        choices=LIQUIDITY_CHOICES,
        null=True,
        blank=True,
    )
    price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    dcf = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    rsi = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    technical_score = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    option_exp = models.DateField(null=True, blank=True)
    option_volume = models.BigIntegerField(null=True, blank=True)
    option_iv = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    next_earnings_date = models.DateField(null=True, blank=True)
    option_data = models.JSONField(null=True, blank=True)
    roi = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    seeking_alpha_ticker_id = models.IntegerField(null=True, blank=True)
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
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "symbol"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"{self.symbol} {self.rating} ({self.created_at:%Y-%m-%d})"


class EmailVerificationToken(models.Model):
    """Single-use email verification token for a user account."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="email_verification_tokens",
        on_delete=models.CASCADE,
    )
    token = models.UUIDField(default=uuid.uuid4, unique=True, editable=False, db_index=True)
    expires_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"{self.user} email verification token"
