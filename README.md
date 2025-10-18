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

## Running tests

```bash
python manage.py test
```
