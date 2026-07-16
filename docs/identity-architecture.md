# Identity architecture

Tài liệu này là bản tóm tắt ở phạm vi backend. Trong workspace đầy đủ, mô tả toàn hệ thống, Flarum extension, security boundary, lifecycle và roadmap nằm tại `docs/shared-auth-architecture.md` ở thư mục gốc.

## Ownership

| Concern | Source of truth |
|---|---|
| Email, password, verification, suspension, deletion lifecycle | Core Auth database |
| Website profiles and future business data | Website/backend database, keyed by Core UUID |
| Forum posts, groups, moderator/admin permissions | Flarum database |
| Core-to-Flarum identity link | Flarum `login_providers`, provider `nfv`, identifier = Core UUID |

Core roles are not copied into Flarum admin groups. An existing Flarum administrator keeps admin access when the verified Core email is linked to that same Flarum row.

## Login flow

1. Flarum redirects the browser to Core Auth `/oauth/authorize` with state and a PKCE challenge.
2. Core Auth handles registration/login and requires verified, active identity state.
3. Core Auth returns a short-lived authorization code to Flarum.
4. Flarum exchanges the code server-to-server and calls `/api/v1/me` with `profile email` scopes.
5. FoF OAuth resolves provider `nfv` + Core UUID. On first login it may safely link an existing Flarum row by verified email; otherwise it creates the forum row.
6. Flarum creates its own session. Core credentials and tokens are never stored in the Flarum user table.

## Lifecycle

`users.status` supports `pending_verification`, `active`, `suspended`, `deletion_requested`, and `anonymized`. API access and OAuth authorization require `active` plus verified email.

`identity_events` is an outbox for later reliable propagation to consumers. The current phase emits activation events and blocks destructive Flarum-side account actions. Before exposing suspension/deletion in an admin UI, add an idempotent outbox worker and a signed Flarum lifecycle endpoint so active Flarum sessions are revoked promptly.

Never hard-delete a Core UUID referenced by posts or business data. Use status transitions and anonymization.

## Security decisions

- UUID is the stable cross-system subject (`sub`). Email is mutable and only a first-link hint.
- Website and Flarum receive separate OAuth clients and secrets.
- Access tokens default to 60 minutes; refresh tokens default to 30 days.
- Cross-subdomain cookies are intentionally not shared.
- Local Flarum password login, password registration, user deletion, and provider unlinking are blocked by the NFV extension.
