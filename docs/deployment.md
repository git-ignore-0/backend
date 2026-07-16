# Deployment runbook (for the later hosting session)

## Preflight

1. Back up the Flarum files and database.
2. Run `php flarum info` and record the exact Flarum/FoF OAuth versions. The extension accepts Flarum 1.8 stable and the 2.x beta line, but must be resolved against the forum's existing root `composer.lock` without forcing a Flarum upgrade.
3. Choose the Core Auth subdomain and point DNS/TLS to Laravel's `public` directory.
4. Create a dedicated MySQL database/user for Core Auth. Do not place Core tables in the Flarum database.
5. Verify SMTP credentials for `no-reply@naturalfarmingvietnam.com` and DNS SPF/DKIM/DMARC.

## Core Auth

Deploy with a production `.env`, then run:

```bash
composer install --no-dev --optimize-autoloader
php artisan key:generate
php artisan passport:keys
php artisan migrate --force
php artisan optimize
```

Point the web root at `backend/public`. Ensure `storage` and `bootstrap/cache` are writable and schedule `php artisan schedule:run` every minute. Keep Passport private keys outside Git and readable only by the application user.

Create a separate confidential Passport client for Flarum and record the exact ID, secret and redirect URI.

## Flarum

Install the extension through a Composer path/VCS repository. Enable FoF OAuth first, then NFV Core SSO. Configure its Core URL/client credentials and clear cache:

```bash
php flarum migrate
php flarum cache:clear
```

Before enabling the extension, create and verify a Core account whose email exactly matches the current Flarum admin. In a private browser, prove that it returns to the same Flarum admin row and retains the admin group. Only then test a new normal member.

## Acceptance checks

- register in Core, receive verification mail, verify, then enter Flarum without another password
- an unverified or suspended Core user cannot authorize or use `/api/v1/me`
- existing Flarum admin maps to the existing row and remains admin
- new user receives a normal member row, never the admin group
- local Flarum password login and password registration return 403
- Flarum user deletion and NFV provider unlink return 403
- logout/login, expired state, invalid callback, refresh, and mobile browser paths work
- no client secret, app key, SMTP password, or Passport private key is present in Git/log output

## Rollback

Disable `nfv/core-sso`, clear Flarum cache, and restore the pre-change Flarum backup if schema/data changed. Core Auth is isolated in its own database, so rollback does not require deleting its data. Keep the old Flarum login path available only during the controlled deployment window; once SSO is verified, the extension enforces Core-only login.
