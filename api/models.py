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

    RISK_LEVEL_CHOICES = [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
    ]

    name = models.CharField(max_length=255)
    ticker = models.CharField(max_length=10, unique=True)
    category = models.CharField(max_length=100)
    risk_level = models.CharField(max_length=10, choices=RISK_LEVEL_CHOICES)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return f"{self.name} ({self.ticker})"
