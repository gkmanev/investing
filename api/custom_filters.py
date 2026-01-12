"""Custom filters applied to all screeners and fetches."""

from __future__ import annotations

EXCHANGE_FILTER_PAYLOAD = {
    "exchange": {
        "include": [
            "New York Stock Exchange(NYSE)",
            "Nasdaq Global Select(NasdaqGS)",
            "Nasdaq Global Market(NasdaqGM)",
            "The Toronto Stock Exchange(TSX)",
        ]
    }
}

CUSTOM_FILTER_PAYLOAD = {
    **EXCHANGE_FILTER_PAYLOAD,
    "marketcap_display": {"gte": 5_000_000_000},
    "quant_rating": {"include": ["buy", "strong_buy"]},
    "sell_side_rating": {"include": ["hold", "buy", "strong_buy"]},
    "authors_rating": {"include": ["hold", "buy", "strong_buy"]},
    "profitability_category": {"include": ["A+", "A", "A-", "B+", "B"]},
    "growth_category": {"include": ["A+", "A", "A-", "B+", "B"]},
    "eps_revisions_category": {"include": ["A+", "A", "A-", "B+", "B"]},
    "value_category": {
        "include": ["A+", "A", "A-", "B+", "B", "B-", "C+", "C"],
    },
    "altman_z_score": {"gte": 2},
    "cash_from_operations_as_reported": {"gte": 0},
}
