"""Custom filters applied to all screeners and fetches."""

from __future__ import annotations

EXCHANGE_FILTER_PAYLOAD = {
    "exchange": {
        "in": [
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
    "quant_rating": {"in": ["buy", "strong_buy"]},
    "sell_side_rating": {"in": ["hold", "buy", "strong_buy"]},
    "authors_rating": {"in": ["hold", "buy", "strong_buy"]},
    "profitability_category": {"in": ["A+", "A", "A-", "B+", "B"]},
    "growth_category": {"in": ["A+", "A", "A-", "B+", "B"]},
    "eps_revisions_category": {"in": ["A+", "A", "A-", "B+", "B"]},
    "value_category": {
        "in": ["A+", "A", "A-", "B+", "B", "B-", "C+", "C"],
    },
    "altman_z_score": {"gte": 2},
    "cash_from_operations_as_reported": {"gte": 0},
}

CUSTOM_FILTER_PAYLOAD_V2 = {
    **EXCHANGE_FILTER_PAYLOAD,
    "marketcap_display": {"gte": 5_000_000_000},
    "quant_rating": {"in": ["buy", "strong_buy"]},
}
