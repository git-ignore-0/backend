# Identity architecture

## Ownership

| Concern | Source of truth |
|---|---|
| UUID, email, password, verification, identity status | Django Core Auth DB |
| Core roles | Django groups/permissions |
| Workshop và business data tương lai | Backend business models, liên kết bằng Core UUID |
| Forum posts, sessions, groups, moderator/admin | Flarum DB |
| Core-to-Flarum link | Flarum login provider `nfv`, identifier = Core UUID |

Core role và Flarum group độc lập. Django superuser không tự trở thành Flarum admin.

## Runtime boundaries

- `django-allauth` quản lý signup, email verification, login và password reset.
- `django-oauth-toolkit` quản lý OAuth client, authorization code, access token và refresh token.
- `python-dotenv` chỉ load `.env` local; biến môi trường của process/hosting có độ ưu tiên cao hơn.
- `identity.User` là custom user ngay từ migration đầu, dùng UUID và email làm login identifier.
- allauth `EmailAddress.verified` là nguồn sự thật về email verification.
- `identity.IdentityEvent` là outbox; cron worker và Flarum lifecycle endpoint sẽ được bổ sung trước khi mở suspend/delete cho admin production.
- `identity.AuditLog` ghi các sự kiện bảo mật, hiện gồm login/logout.

Database có check constraint buộc `is_active=true` chỉ đi cùng `pending_verification|active`; các trạng thái `suspended|deletion_requested|anonymized` bắt buộc `is_active=false`.

## Flarum login flow

1. Extension tạo state và PKCE S256 rồi redirect tới `/oauth/authorize`.
2. Core yêu cầu user đăng nhập, xác minh email và có status `active`.
3. Flarum đổi authorization code tại `/oauth/token`.
4. Flarum gọi `/api/v1/me` với Bearer token có scopes `profile email`.
5. Response trả Core UUID trong `data.sub`; Flarum dùng UUID này làm stable provider identifier.
6. Flarum tạo session riêng và giữ nguyên group/admin của forum row đã liên kết.

## Account lifecycle

`transition_account()` khóa row trong transaction, xác minh transition hợp lệ, cập nhật `status` cùng `is_active` và ghi outbox event. Identity đã anonymized là trạng thái kết thúc; identity chưa xác minh email không được chuyển sang active. User chỉ được API/SSO chấp nhận khi:

```text
status = active
AND is_active = true
AND primary email is verified
```

Không hard-delete Core UUID. Ở phase hiện tại Django Admin đã khóa create/delete và các field `email`, `status`, `is_active`. User thường phải đăng ký qua Core signup; superuser được tạo bằng `createsuperuser`. Chỉ mở thao tác suspend/delete trong admin sau khi lifecycle worker thu hồi được Flarum session.

## API contract

```http
GET /api/v1/me
Authorization: Bearer <access-token>
```

Token thiếu `profile email` trả `403 insufficient_scope`; token sai/hết hạn trả `401 invalid_token`; identity không active trả `403 account_inactive`.

Mọi response `/api/v1/me` dùng `Cache-Control: no-store`. Lỗi Bearer trả `WWW-Authenticate` phù hợp. Authorization code dùng PKCE; Core validator tự enforce tuổi refresh token 30 ngày, rotation và reuse protection. Lệnh `cleartokens` chỉ còn nhiệm vụ dọn database. Production chỉ cho HTTPS redirect URI.

```json
{
  "data": {
    "sub": "core-user-uuid",
    "name": "Nguyen Van A",
    "email": "user@example.com",
    "email_verified": true,
    "locale": "vi",
    "status": "active",
    "roles": ["member"]
  }
}
```
