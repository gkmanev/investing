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

    class TechnicalScore(models.TextChoices):
        STRONG_BUY = "Strong Buy", "Strong Buy"
        BUY = "Buy", "Buy"
        NEUTRAL = "Neutral", "Neutral"
        SELL = "Sell", "Sell"
        STRONG_SELL = "Strong Sell", "Strong Sell"

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
    technical_score = models.CharField(
        max_length=16,
        choices=TechnicalScore.choices,
        null=True,
        blank=True,
    )
    option_exp = models.DateField(null=True, blank=True)
    option_volume = models.BigIntegerField(null=True, blank=True)
    option_iv = models.DecimalField(max_digits=10, decimal_places=4, null=True, blank=True)
    next_earnings_date = models.DateField(null=True, blank=True)
    option_data = models.JSONField(null=True, blank=True)
    call_data = models.JSONField(null=True, blank=True)
    roi = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    seeking_alpha_ticker_id = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ticker"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return self.ticker


class DailyBrief(models.Model):
    """Stores the ranked daily brief picks generated from Symbol snapshots."""

    edition_date = models.DateField(db_index=True)
    rank = models.PositiveSmallIntegerField()
    symbol = models.ForeignKey(
        Symbol,
        related_name="daily_briefs",
        on_delete=models.PROTECT,
    )
    ticker = models.CharField(max_length=50, db_index=True)
    score = models.IntegerField(null=True, blank=True)
    rsi = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    roi = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    delta = models.DecimalField(max_digits=7, decimal_places=4, null=True, blank=True)
    is_alternative = models.BooleanField(default=False, db_index=True)
    option_data = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-edition_date", "rank", "ticker"]
        constraints = [
            models.UniqueConstraint(
                fields=["edition_date", "rank"],
                name="unique_daily_brief_rank_per_day",
            ),
            models.UniqueConstraint(
                fields=["edition_date", "symbol"],
                name="unique_daily_brief_symbol_per_day",
            ),
        ]

    @classmethod
    def edition_rows(cls, *, edition_date):
        return cls.objects.filter(edition_date=edition_date).select_related("symbol").order_by(
            "rank",
            "ticker",
        )

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"{self.edition_date:%Y-%m-%d} #{self.rank} {self.ticker}"


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


class AgentRun(models.Model):
    """Tracks a persisted agent execution request and outcome."""

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        COMPLETED = "completed", "Completed"
        FAILED = "failed", "Failed"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="agent_runs",
        on_delete=models.CASCADE,
    )
    query = models.TextField()
    history_json = models.JSONField(default=list, blank=True)
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    result_text = models.TextField(blank=True, default="")
    error_text = models.TextField(blank=True, default="")
    used_tools_json = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(null=True, blank=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"Agent run #{self.pk} for user {self.user_id} ({self.status})"


class DailyBriefSubscription(models.Model):
    """Tracks a user's Daily Top 3 email subscription state."""

    class Status(models.TextChoices):
        PENDING_VERIFICATION = "pending_verification", "Pending verification"
        ACTIVE = "active", "Active"
        UNSUBSCRIBED = "unsubscribed", "Unsubscribed"

    class Source(models.TextChoices):
        UNKNOWN = "", "Unknown"
        SIGNUP = "signup", "Signup"
        DAILY_BRIEF_CTA = "daily_brief_cta", "Daily brief CTA"
        PROFILE = "profile", "Profile"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="daily_brief_subscription",
        on_delete=models.CASCADE,
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.UNSUBSCRIBED,
        db_index=True,
    )
    is_active = models.BooleanField(default=False, db_index=True)
    source = models.CharField(
        max_length=32,
        choices=Source.choices,
        blank=True,
        default=Source.UNKNOWN,
        db_index=True,
    )
    subscribed_at = models.DateTimeField(null=True, blank=True, db_index=True)
    unsubscribed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-subscribed_at", "user_id"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"{self.user} daily brief ({self.status})"


class DailyBriefEdition(models.Model):
    """Stores the single Daily Top 3 edition generated for a calendar day."""

    edition_date = models.DateField(unique=True, db_index=True)
    subject = models.CharField(max_length=255)
    body_text = models.TextField()
    top_symbols = models.JSONField(default=list, blank=True)
    recipient_count = models.PositiveIntegerField(default=0)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-edition_date"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"Daily brief edition {self.edition_date:%Y-%m-%d}"


class PremiumSubscription(models.Model):
    """Tracks a user's premium (paid) subscription managed via Stripe."""

    class Status(models.TextChoices):
        ACTIVE = "active", "Active"
        TRIALING = "trialing", "Trialing"
        PAST_DUE = "past_due", "Past due"
        PAUSED = "paused", "Paused"
        CANCELLED = "cancelled", "Cancelled"

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        related_name="premium_subscription",
        on_delete=models.CASCADE,
    )
    stripe_subscription_id = models.CharField(max_length=255, unique=True)
    stripe_customer_id = models.CharField(max_length=255, blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.ACTIVE,
        db_index=True,
    )
    current_period_end = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def is_active(self) -> bool:
        return self.status in {self.Status.ACTIVE, self.Status.TRIALING}

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"{self.user} premium ({self.status})"
