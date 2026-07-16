# Natural Farming Vietnam Core Auth

Django service sở hữu tài khoản chung và phát OAuth2 identity cho website chính và Flarum.

## Phạm vi hiện tại

- custom user UUID với vòng đời `pending_verification`, `active`, `suspended`, `deletion_requested`, `anonymized`;
- đăng ký, xác minh email, đăng nhập và reset mật khẩu qua django-allauth;
- OAuth2 Authorization Code + PKCE qua Django OAuth Toolkit;
- contract tương thích Flarum tại `GET /api/v1/me` với scopes `profile email`;
- audit log và transactional identity-event outbox;
- Django Admin không cho hard-delete identity;
- giao diện auth server-rendered, không cần Node build.

Flarum vẫn sở hữu bài viết, session, group và quyền admin/moderator của forum trong database riêng.

## Chạy local

Yêu cầu Python 3.11+.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
.venv/bin/python manage.py migrate
.venv/bin/python manage.py createsuperuser
.venv/bin/python manage.py runserver
```

Mặc định dùng SQLite và in email ra terminal. Link xác minh xuất hiện trong output của `runserver`.

Kiểm tra code:

```bash
.venv/bin/ruff check .
.venv/bin/python manage.py check
.venv/bin/python manage.py makemigrations --check
.venv/bin/pytest
```

## Tạo OAuth client cho Flarum

Vào `/admin/oauth2_provider/application/add/` và tạo application:

- Client type: `Confidential`;
- Authorization grant type: `Authorization code`;
- Redirect URI: callback chính xác do FoF OAuth hiển thị;
- Algorithm: `No OIDC support` nếu chỉ dùng OAuth2 hiện tại.

Copy client ID và client secret ngay khi tạo, rồi lưu trong Flarum/hosting secrets. Không commit secret. Mỗi website, forum và admin frontend phải dùng client riêng.

Các endpoint public tương thích extension:

```text
/oauth/authorize
/oauth/token
/api/v1/me
```

Xem thêm [identity-architecture.md](docs/identity-architecture.md) và [deployment.md](docs/deployment.md).

## Lịch sử chuyển đổi

Prototype Laravel được giữ ở branch `laravel-prototype`. Bản Django được phát triển trên branch `python-rewrite` rồi fast-forward vào `main`; không xóa branch prototype cho tới khi local integration và deploy production hoàn tất.
