# Natural Farming Vietnam Core Auth

Central account and OAuth2 service for `naturalfarmingvietnam.com` and `forum.naturalfarmingvietnam.com`.

Workspace-level architecture is documented in `docs/shared-auth-architecture.md` at the Natural Farming Vietnam workspace root.

## Current scope

- UUID user identities with lifecycle states
- registration, email verification, password reset and optional 2FA through Laravel Fortify
- OAuth2 Authorization Code flow with PKCE through Laravel Passport
- scoped `GET /api/v1/me` identity endpoint
- audit log and transactional identity-event outbox tables
- server-rendered Vietnamese-first account screens (no frontend build required)

Core Auth owns credentials and identity state. Flarum keeps a separate application database and its own groups/admin permissions. Flarum links its user row to the immutable Core UUID through FoF OAuth's login-provider mapping.

## Local setup

Requirements: PHP 8.3+, Composer, and Laravel's required PHP extensions. Node is not required for the current server-rendered UI.

```bash
cp .env.example .env
composer install
php artisan key:generate
touch database/database.sqlite
php artisan migrate
php artisan passport:keys
php artisan serve
```

Email is logged locally. Registration links can be inspected in `storage/logs/laravel.log` until SMTP is configured.

Run verification:

```bash
composer test
./vendor/bin/pint --test
```

## OAuth clients

Create one confidential authorization-code client per application. Do not reuse a secret across the website and forum.

```bash
php artisan passport:client
```

For Flarum, register the exact callback URL displayed by FoF OAuth. The client requests `profile email` and uses PKCE. Store the client ID and secret only in Flarum settings/hosting secrets, never in this repository.

See [deployment.md](docs/deployment.md) and [identity-architecture.md](docs/identity-architecture.md). Do not deploy with `APP_DEBUG=true`, SQLite, log mail, or generated local Passport keys.
