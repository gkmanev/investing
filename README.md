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

## Authentication

The API now supports JWT authentication for frontend apps with email verification.

Endpoints:

- `POST /api/auth/register/` creates a new inactive user and sends a verification email when `AUTH_ALLOW_PUBLIC_REGISTRATION=True`
  - Accepts optional `daily_brief_opt_in: true|false` for Daily Top 3 signup intent
- `POST /api/auth/login/` with `{"identifier": "<username-or-email>", "password": "<password>"}` after the email is verified
- `POST /api/auth/verify-email/` with `{"token": "<uuid-from-email-link>"}`
- `POST /api/auth/resend-verification/` with `{"identifier": "<username-or-email>"}`
- `POST /api/auth/refresh/` uses the refresh token from an `HttpOnly` cookie
- `POST /api/auth/logout/` clears the refresh cookie and blacklists the token
- `GET /api/auth/me/` returns the authenticated user
- `GET /api/daily-brief-subscription/` returns the authenticated user's Daily Top 3 subscription status
- `POST /api/daily-brief-subscription/subscribe/` activates or queues the Daily Top 3 subscription
- `POST /api/daily-brief-subscription/unsubscribe/` disables the Daily Top 3 subscription

Frontend flow:

1. Call `POST /api/auth/register/`
2. Registration returns a verification-required response and sends an email
3. Open the frontend verification page from the email link and call `POST /api/auth/verify-email/`
4. Store the returned access token in memory
5. Send `Authorization: Bearer <access-token>` on authenticated API requests
6. Send requests with credentials enabled so the refresh cookie is included
7. On reload or `401`, call `POST /api/auth/refresh/` to get a new access token

Write operations on the existing API viewsets are restricted to staff users. Read operations remain public.

Email delivery:

- If `RESEND_API_KEY` is configured, verification emails are sent through the Resend HTTPS API.
- If `RESEND_API_KEY` is not configured, the project falls back to Django's configured email backend.
- Daily Top 3 delivery is scheduled through Celery Beat using `api.tasks.send_daily_top_3_edition`.
- `trading_view_scrape` is scheduled through Celery Beat every 45 minutes from 13:00 UTC through 19:45 UTC using `api.tasks.run_trading_view_scrape`.
- `initial_screener` is scheduled daily at 12:30 UTC using `api.tasks.run_initial_screener`.
- Adjust the UTC send time with `DAILY_BRIEF_SEND_HOUR_UTC` and `DAILY_BRIEF_SEND_MINUTE_UTC`.
- For Railway testing, you can manually preview or send with `python manage.py send_daily_brief --dry-run --limit 3` and `python manage.py send_daily_brief --limit 3`.

## Deploying with Docker Compose

1. Copy the example environment file and update values:

```bash
cp .env.example .env
```

Set `ALLOWED_HOSTS` to include your VM IP (for example, `209.38.208.230`) and update
`SECRET_KEY` for production.

2. Build and start the service:

```bash
docker compose up -d --build
```

This now starts four containers:

- `web` for Django + Gunicorn
- `celery_worker` for asynchronous tasks
- `celery_beat` for scheduled tasks
- `redis` as the Celery broker/result backend

The API will be available at `http://209.38.208.230:8080/api/investments/`.

If you want to override the broker location, set:

```bash
CELERY_BROKER_URL=redis://redis:6379/0
CELERY_RESULT_BACKEND=redis://redis:6379/0
```

## Railway services

The Docker image now auto-selects its role from `SERVICE_ROLE` or, on Railway, from
`RAILWAY_SERVICE_NAME`.

Use three separate Railway services from the same image:

- Web service: set `SERVICE_ROLE=web`
- Celery worker service: set `SERVICE_ROLE=worker`
- Celery beat service: set `SERVICE_ROLE=beat`

Recommended environment for Railway:

```bash
CELERY_WORKER_CONCURRENCY=2
```

`celery beat` uses the Django database scheduler, so it must start only after the
`django_celery_beat` tables exist. With this image, the web service runs migrations
and the beat service waits for those tables instead of crashing during startup.

To view logs:

```bash
docker compose logs -f
```

To stop:

```bash
docker compose down
```

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
```

When requesting the custom screener filter, the response includes the number of returned tickers:

```bash
curl "http://127.0.0.1:8000/api/investments/?screener_type=Custom%20screener%20filter"
```

Using the legacy `screenter_type` query parameter works the same way:

```bash
curl "http://127.0.0.1:8000/api/investments/?screenter_type=growth"
```

Find ETFs with a minimum price of $10 and minimum volume of 1,000:

```bash
curl "http://127.0.0.1:8000/api/investments/?category=ETF&min_price=10&min_volume=1000"
```

## Running tests

```bash
python manage.py test
```
