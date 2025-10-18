from django.db import models


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
