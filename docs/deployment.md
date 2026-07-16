# Django deployment runbook

Chưa chạy các bước này cho tới phiên deploy có thông tin hosting, SMTP và domain thật.

## Hosting preflight

Xác nhận cPanel có Setup Python App/Passenger, Python 3.11+, virtualenv, pip, cron và cách restart application. Tạo database/user riêng cho Core Auth; không dùng database Flarum. Production dùng MySQL Connector/Python dạng wheel để không phụ thuộc compiler hoặc Python development headers trên shared hosting. Backend nội bộ `config.db.backends.mysql_connector` vá tương thích `display_name` và schema-literal quoting giữa Connector/Python 9.7 và Django 5.2.

## Cài ứng dụng

Ví dụ trong application root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[mysql]'
cp .env.example .env
.venv/bin/python manage.py migrate
.venv/bin/python manage.py collectstatic --noinput
.venv/bin/python manage.py check --deploy
```

File `locale/vi/LC_MESSAGES/django.mo` phải có trong bản deploy. Production không cần GNU gettext hoặc `polib`; chỉ cần compile lại bằng `scripts/compile_translations.py` ở môi trường phát triển khi file `.po` thay đổi.

Passenger entrypoint là `passenger_wsgi.py`. Nếu hosting dùng Gunicorn thì WSGI target là `config.wsgi:application`.

Production `.env` tối thiểu:

```text
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=auth.naturalfarmingvietnam.com
DJANGO_CSRF_TRUSTED_ORIGINS=https://auth.naturalfarmingvietnam.com
DB_ENGINE=mysql
DJANGO_SECURE_SSL_REDIRECT=true
DJANGO_SESSION_COOKIE_SECURE=true
DJANGO_CSRF_COOKIE_SECURE=true
DJANGO_SECURE_HSTS_SECONDS=31536000
DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS=true
```

Chỉ bật `DJANGO_BEHIND_PROXY=true` khi cPanel xác nhận HTTPS được terminate ở trusted reverse proxy và gửi `X-Forwarded-Proto` đúng cách. Chỉ bật HSTS preload sau khi mọi subdomain đều dùng HTTPS ổn định.

Sinh `DJANGO_SECRET_KEY` riêng ngoài Git. Cấu hình SMTP cho `no-reply@naturalfarmingvietnam.com`, SPF/DKIM/DMARC và không log password/token.
SMTP port 465 phải dùng `EMAIL_USE_SSL=true` và `EMAIL_USE_TLS=false`; port 587 STARTTLS dùng cấu hình ngược lại. Backend fail-fast nếu cả hai cùng được bật.

Ứng dụng fail-fast khi production thiếu `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` hoặc thông tin MySQL bắt buộc. Không copy `.env` Laravel hay Passport keys sang deployment Django.

Schedule dọn OAuth token hết hạn, ví dụ cron hằng ngày:

```bash
.venv/bin/python manage.py cleartokens
```

## OAuth/Flarum

1. Tạo Django superuser bằng `manage.py createsuperuser`.
2. Trong Django Admin, tạo confidential authorization-code application cho Flarum.
3. Redirect URI phải khớp tuyệt đối callback FoF OAuth.
4. Cấu hình Core URL, client ID và secret trong Flarum.
5. Tạo/verify Core account có email trùng Flarum admin hiện tại.
6. Bật `nfv-core-sso` rồi test trong cửa sổ ẩn danh trước khi chặn login local.

Django Admin không cho tạo/xóa user hoặc sửa email/status ở phase hiện tại. User đăng ký qua Core signup; superuser tạo bằng CLI. Đây là khóa an toàn cho tới khi outbox worker và Flarum lifecycle endpoint hoàn tất.

## Acceptance checks

- đăng ký → nhận email → verify → status active → vào Flarum không cần password thứ hai;
- pending/suspended user không dùng được OAuth identity API;
- Flarum admin cũ map đúng row và giữ group admin;
- member mới không nhận quyền admin;
- `/oauth/authorize`, `/oauth/token`, `/api/v1/me` hoạt động trên HTTPS;
- static files, reset password, callback lỗi, expired token và mobile browser hoạt động;
- đổi `VI / EN` trên login, signup, verification, reset password và OAuth consent; refresh vẫn giữ đúng ngôn ngữ;
- email verification/reset có subject và body đúng ngôn ngữ đã chọn của user;
- Git/log không chứa database password, SMTP password, Django secret hay OAuth client secret.
- `pip-audit` không báo dependency vulnerability đã biết;
- `manage.py check --deploy`, migration check và toàn bộ pytest đều pass.

## Rollback

Tắt extension `nfv/core-sso`, clear Flarum cache và phục hồi Flarum backup nếu cần. Core Auth dùng database riêng nên rollback forum không yêu cầu xóa dữ liệu Core. Branch `laravel-prototype` chỉ là bản tham chiếu local, không dùng làm rollback production sau khi Django đã sinh dữ liệu thật.
