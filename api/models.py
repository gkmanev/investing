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
    price = models.DecimalField(max_digits=20, decimal_places=4, null=True, blank=True)
    volume = models.BigIntegerField(null=True, blank=True)
    market_cap = models.DecimalField(max_digits=24, decimal_places=2, null=True, blank=True)
    options_suitability = models.BooleanField(default=False)
    description = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["ticker"]

    def __str__(self) -> str:  # pragma: no cover - simple data representation
        return self.ticker
