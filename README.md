# Natural Farming Vietnam Backend

Django service sở hữu Core Identity/OAuth2, public content API và CMS API.

Tài liệu canonical không được duy trì trong project README hoặc `backend/docs/`:

- kiến trúc, model, endpoint và contract: [`../docs/backend.md`](../docs/backend.md);
- setup local và quality gate: [`../docs/development-deployment.md`](../docs/development-deployment.md);
- deploy production/rollback: [`../docs/backend-deployment-runbook.md`](../docs/backend-deployment-runbook.md);
- ranh giới toàn hệ thống: [`../docs/architecture-overview.md`](../docs/architecture-overview.md).

Dependency và runtime version lấy từ [`pyproject.toml`](pyproject.toml). Mọi lệnh phải chạy trong
`backend/`; không dùng `.env` cũ của Laravel và không commit secret.
