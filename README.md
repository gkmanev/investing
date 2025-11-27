# Investing API

A simple Django REST Framework project that exposes an API for managing investment options.

## Requirements

- Python 3.11+
- Django 4.2
- Django REST Framework 3.16

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

## Running the project

Apply migrations and start the development server:

```bash
python manage.py migrate
python manage.py runserver
```

The API will be available at `http://127.0.0.1:8000/api/investments/`.

## Filtering investments

You can refine the investment list by passing query parameters:

- `ticker` – partial, case-insensitive match on the ticker symbol.
- `category` – exact, case-insensitive match on the investment category.
- `screener_type` – exact, case-insensitive match on the screener type (for example, `growth` or `value`). If the screener type contains spaces, URL-encode them (e.g., `Strong%20Buy%20Stocks%20With%20Short%20Squeeze%20Potential`). The legacy `screenter_type` query parameter is still accepted for backward compatibility.
- `options_suitability` – exact integer match for options suitability (for example, `0` or `1`).
- Numeric range filters – use `min_price`, `max_price`, `min_market_cap`, `max_market_cap`, `min_volume`, and `max_volume`.

### Examples

List only growth screener investments suitable for options:

```bash
curl "http://127.0.0.1:8000/api/investments/?screener_type=growth&options_suitability=1"
```

Filter by a screener type that includes spaces:

```bash
curl "http://127.0.0.1:8000/api/investments/?screener_type=Strong%20Buy%20Stocks%20With%20Short%20Squeeze%20Potential"

Using the legacy `screenter_type` query parameter works the same way:

```bash
curl "http://127.0.0.1:8000/api/investments/?screenter_type=growth"
```
```

Find ETFs with a minimum price of $10 and minimum volume of 1,000:

```bash
curl "http://127.0.0.1:8000/api/investments/?category=ETF&min_price=10&min_volume=1000"
```

## Running tests

```bash
python manage.py test
```
