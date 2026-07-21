# Natural Farming Vietnam Backend

Django backend dùng chung cho hai miền nghiệp vụ:

1. **Core Identity/OAuth**: tài khoản UUID, đăng ký/đăng nhập/xác minh email, OAuth2 Authorization Code + PKCE, `/api/v1/me`, audit và lifecycle outbox.
2. **Website Content/CMS**: public API cho site settings/workshop, admin API, revision/publish/preview, upload ảnh và cache revalidation.

## Công nghệ

- Python `>=3.11`, Django `5.2`
- django-allauth, Django OAuth Toolkit
- Django REST Framework
- Pillow cho image processing
- SQLite local; MySQL production qua optional dependency `mysql`
- Django templates cho auth UI; không cần Node build

## Chạy local

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Không tái sử dụng `.env` từ Laravel prototype cũ. Local mặc định dùng SQLite và console email backend; link xác minh/reset password xuất hiện trong terminal.

## Runtime chính

- `config/settings.py`: env, database, middleware, auth, OAuth, DRF, media và security.
- `config/urls.py`: route identity, OAuth, content API, health và Django Admin.
- `identity/`: user lifecycle, allauth integration, OAuth guards, audit và webhook outbox.
- `content/`: site settings, workshop revisions, public/admin APIs, upload và revalidation.
- `templates/`, `static/`, `locale/`: auth UI song ngữ server-rendered.

## Endpoint chính

```text
GET|POST /register, /login, /accounts/...
GET|POST /oauth/authorize
POST     /oauth/token
GET      /api/v1/me

GET      /api/v1/public/site-settings
GET      /api/v1/public/workshops
GET      /api/v1/public/workshops/<slug>
GET      /api/v1/public/workshops/preview/<token>

GET      /api/v1/admin/session
GET      /api/v1/admin/dashboard
GET|PATCH /api/v1/admin/site-settings
POST     /api/v1/admin/media
REST     /api/v1/admin/workshops/
```

Admin content API yêu cầu access token có scope `content:manage` và user thuộc Django group `website_admin`. `/api/v1/me` yêu cầu đồng thời scopes `profile email`.

## Identity lifecycle

`identity.User` dùng UUID và các trạng thái:

```text
pending_verification -> active -> suspended | deletion_requested -> anonymized
```

Eligibility cho OAuth/Core API là `status=active`, `is_active=true` và email hiện tại đã verified trong allauth. Lifecycle change phải đi qua `identity.services.transition_account()` để giữ constraint và tạo `IdentityEvent` cùng transaction.

Gửi event sang Flarum bằng cron:

```bash
.venv/bin/python manage.py dispatch_identity_events --limit 25
```

## Workshop publishing

Workshop có một draft revision và tối đa một published revision. Save/publish dùng optimistic version; publish validate bản dịch ready, chuyển draft thành published, clone draft mới, ghi audit và gửi signed revalidation sang frontend sau commit. Preview dùng signed token hết hạn 10 phút và không yêu cầu login ở public preview endpoint.

## Dịch thuật

Khi sửa `locale/vi/LC_MESSAGES/django.po`, compile và commit cả `.po` lẫn `.mo`:

```bash
.venv/bin/python scripts/compile_translations.py
```

## Kiểm tra

```bash
.venv/bin/ruff format --check config content identity scripts tests manage.py passenger_wsgi.py
.venv/bin/ruff check .
.venv/bin/python manage.py makemigrations --check
.venv/bin/python manage.py check
.venv/bin/pytest -W error
.venv/bin/pip-audit
```

## Production

Production thường dùng MySQL, WhiteNoise cho static và Passenger (`passenger_wsgi.py`) hoặc Gunicorn (`config.wsgi:application`). Trước deploy:

```bash
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py check --deploy
```

Ngoài web process, cần cron dispatch identity events và dọn OAuth tokens (`manage.py cleartokens`). Secrets OAuth, lifecycle và revalidation không được commit.

Xem chi tiết tại [`../docs/backend.md`](../docs/backend.md) và [`../docs/development-deployment.md`](../docs/development-deployment.md).
