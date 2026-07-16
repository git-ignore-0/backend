# Natural Farming Vietnam Core Auth

Django service sở hữu tài khoản chung và phát OAuth2 identity cho website chính và Flarum. Flarum vẫn giữ database, session, bài viết, group và quyền admin/moderator riêng.

## Phạm vi hiện tại

- custom user UUID với vòng đời `pending_verification`, `active`, `suspended`, `deletion_requested`, `anonymized`;
- đăng ký, xác minh email, đăng nhập, đăng xuất và reset mật khẩu qua django-allauth;
- OAuth2 Authorization Code + PKCE qua Django OAuth Toolkit;
- contract tương thích Flarum tại `GET /api/v1/me`, bắt buộc scopes `profile email`;
- audit log và transactional identity-event outbox;
- Django Admin chỉ quản lý profile/role: không tạo, hard-delete, đổi email hoặc đổi lifecycle status ở phase hiện tại;
- giao diện auth server-rendered, không cần Node build.

## Cấu trúc

```text
backend/
├── config/                 Django settings, URL, WSGI/ASGI
├── identity/               User, auth rules, OAuth guard, audit/outbox
│   └── migrations/         Database migrations
├── templates/              Login, signup, verification, consent
├── static/                 Source static assets
├── tests/                  Integration và domain tests
├── docs/                   Architecture và deployment runbook
├── manage.py
├── passenger_wsgi.py
└── pyproject.toml
```

`staticfiles/`, SQLite local, virtualenv, cache và `.env` là artifact local nên không được commit. Laravel `vendor/`, `bootstrap/`, `storage/`, Passport keys và Composer files không thuộc implementation Django.

## Chạy local

Yêu cầu Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

`python-dotenv` tự load file `.env`; environment variables do hosting/process cung cấp luôn được ưu tiên hơn file này. Mặc định local dùng SQLite và in email ra terminal, nên link xác minh xuất hiện trong output của `runserver`.

Nếu workspace từng chạy Laravel, không tái sử dụng `.env` cũ. Hãy backup nó ngoài repo rồi tạo `.env` mới từ `.env.example`.

## Kiểm tra code

```bash
.venv/bin/ruff format --check config identity tests manage.py passenger_wsgi.py
.venv/bin/ruff check .
.venv/bin/python manage.py makemigrations --check
.venv/bin/python manage.py check
.venv/bin/pytest -W error
.venv/bin/pip-audit
```

Test hiện bao phủ đăng ký/xác minh, login theo trạng thái, DB lifecycle constraint, audit/outbox, OAuth endpoint compatibility và toàn bộ Authorization Code + PKCE → access token → `/api/v1/me`.

## Tạo OAuth client cho Flarum

Cách 1: vào `/admin/oauth2_provider/application/add/` và tạo application:

- Client type: `Confidential`;
- Authorization grant type: `Authorization code`;
- Redirect URI: callback chính xác do FoF OAuth hiển thị;
- không bật OIDC khi chưa có yêu cầu.

Cách 2: dùng management command:

```bash
.venv/bin/python manage.py createapplication \
  confidential authorization-code \
  --name "NFV Forum" \
  --redirect-uris "https://forum.naturalfarmingvietnam.com/auth/nfv"
```

Lưu client ID/secret trong Flarum hoặc hosting secrets, không commit Git. Mỗi website, forum và admin frontend phải dùng client riêng. Production chỉ chấp nhận HTTPS redirect URI; HTTP chỉ được phép khi `DJANGO_DEBUG=true` để test local.

Các endpoint public tương thích extension:

```text
/oauth/authorize
/oauth/token
/api/v1/me
```

Xem [identity-architecture.md](docs/identity-architecture.md) và [deployment.md](docs/deployment.md).

## Git history

- `main`: implementation Django hiện tại;
- `python-rewrite`: giữ mốc nhánh chuyển đổi;
- `laravel-prototype`: prototype Laravel cũ, chỉ để tham chiếu và không dùng để deploy.
