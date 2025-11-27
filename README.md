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
- `screenter_type` – exact, case-insensitive match on the screenter type (for example, `growth` or `value`).
- `options_suitability` – exact integer match for options suitability (for example, `0` or `1`).
- Numeric range filters – use `min_price`, `max_price`, `min_market_cap`, `max_market_cap`, `min_volume`, and `max_volume`.

### Examples

List only growth screenter investments suitable for options:

```bash
curl "http://127.0.0.1:8000/api/investments/?screenter_type=growth&options_suitability=1"
```

Find ETFs with a minimum price of $10 and minimum volume of 1,000:

```bash
curl "http://127.0.0.1:8000/api/investments/?category=ETF&min_price=10&min_volume=1000"
```

## Running tests

```bash
python manage.py test
```
