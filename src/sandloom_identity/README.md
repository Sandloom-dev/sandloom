# Identity Module

Модуль аутентификации, авторизации и управления пользователями Sandloom Framework
(обязательный, `required=True`). Полное англоязычное описание:
[`docs/modules/identity.md`](../../../docs/modules/identity.md).

## Возможности

- Регистрация и вход по email/паролю; вход только после подтверждения email.
- JWT access-токены + refresh-токены с ротацией по семействам и детектом
  повторного использования.
- Верификация email, одноразовые ссылки сброса пароля, смена пароля.
- Per-account lockout на пароль и второй фактор.
- 2FA (TOTP + резервные коды) с повторной аутентификацией для изменений.
- SSO: Google, GitHub, Microsoft, LinkedIn (PKCE, OIDC nonce, привязка flow к
  клиенту).
- Нормализованный RBAC (глобальные роли/права), claims в access-токене.
- Отзыв сессий (`revoke_user_tokens` ядра) и доменные события `identity.*`
  без PII; аудит чувствительных действий.

## Структура модуля

```
identity/
├── __init__.py          # register(core): роутеры, схемы событий, claims-контрибьютор
├── access/              # зависимости: get_current_principal, session_not_revoked, is_self_or_admin, ...
├── api/                 # auth, sso, twofa, users, roles, user_roles
├── models/              # User, Role, Permission, UserRole, RolePermission,
│                        # ExternalIdentity, RefreshToken, TwoFASecret, TwoFABackupCode
├── schemas/             # Pydantic DTO
├── services/            # auth, token, user, role, sso, twofa, reauth
├── migrations/          # собственная цепочка миграций модуля
├── claims.py            # contribute_identity_claims (roles/permissions)
├── events.py            # события и их контракт
├── lockout.py           # per-account блокировка подбора
├── sessions.py          # revoke_sessions → identity.user_sessions_revoked.v1
├── integrations.py      # OAuth-клиенты и отправка писем (notifier)
├── password_policy.py
├── utils.py             # токены email/сброса, хэши, шифрование
└── tasks.py             # @shared_task для воркера фреймворка (celery -A sandloom.core.worker)
```

## API Endpoints

### Аутентификация (`/auth`)

```
POST   /auth/register               # 202 всегда одинаковый ответ (без перечисления аккаунтов)
POST   /auth/login                  # 403 для неподтверждённого email; 401 + X-2FA-Required при нужном otp
POST   /auth/refresh                # ротация; повтор ≤10 с после ротации → 409 (гонка), позже → отзыв семейства (401)
POST   /auth/logout
POST   /auth/verify-email
POST   /auth/resend-verification    # всегда 204
POST   /auth/request-password-reset # всегда 204
POST   /auth/reset-password         # одноразовая ссылка
POST   /auth/change-password        # current_password + new_password → новый TokenPair, остальные сессии отозваны
GET    /auth/me
PATCH  /auth/me
```

### Двухфакторная аутентификация (`/auth/2fa`)

```
GET    /auth/2fa/status
POST   /auth/2fa/init                     # нужен current_password
POST   /auth/2fa/enable                   # otp + current_password
POST   /auth/2fa/disable                  # otp + current_password
POST   /auth/2fa/verify                   # с lockout
GET    /auth/2fa/backup-codes
POST   /auth/2fa/backup-codes/regenerate  # otp + current_password
```

Для аккаунтов без пароля (только SSO) вместо `current_password` требуется
access-токен, выпущенный не более 300 секунд назад.

### Single Sign-On (`/auth/sso`)

```
GET    /auth/sso/providers
GET    /auth/sso/{provider}/redirect        # JSON {auth_url, state, binding} + cookie sandloom_sso_binding
GET    /auth/sso/{provider}/callback        # нужен binding (cookie или X-SSO-Binding); TokenPair или MFA-челлендж
POST   /auth/sso/mfa                        # завершение SSO-входа вторым фактором
GET    /auth/sso/{provider}/link/redirect   # начало привязки к текущему аккаунту
POST   /auth/sso/{provider}/link
DELETE /auth/sso/{provider}/unlink
GET    /auth/sso/me/linked
```

Автопривязка по email — только если email подтверждён провайдером И локальный
аккаунт подтверждён. Microsoft идентифицируется по `tid:oid` (email доверяется
только с claim `xms_edov`), GitHub — только verified primary email. Настройка:
[SSO setup](../../../docs/guides/sso-setup.md).

### Пользователи и роли (`/api/v1`)

```
GET    /api/v1/users                    # admin
GET    /api/v1/users/{user_id}          # self или admin
PUT    /api/v1/users/{user_id}          # self или admin (is_active меняет только admin)
DELETE /api/v1/users/{user_id}          # admin, soft delete
GET    /api/v1/users/{user_id}/roles

GET    /api/v1/roles
POST   /api/v1/roles
GET    /api/v1/roles/{role_id}
PUT    /api/v1/roles/{role_id}
DELETE /api/v1/roles/{role_id}
GET    /api/v1/permissions
POST   /api/v1/permissions
POST   /api/v1/roles/{role_id}/permissions/{permission_id}
DELETE /api/v1/roles/{role_id}/permissions/{permission_id}
POST   /api/v1/users/{user_id}/roles/{role_id}
DELETE /api/v1/users/{user_id}/roles/{role_id}
```

## Использование из других модулей

Модуль подключается через `SANDLOOM_MODULES` (`register(core)`), а не импортом
роутера. Другие модули не импортируют `sandloom_identity`; они используют:

```python
from sandloom.sdk import CurrentUser, module_router, require
from sandloom.sdk.authz import Perm, Role

router = module_router()


@router.get("/admin-only", dependencies=[Depends(require(Role("admin")))])
async def admin_endpoint(user: CurrentUser): ...  # JWT, без БД
```

Access-токен содержит `sub`, `email`, `email_verified`, `token_type: "access"`,
`roles`, `permissions` и claims других модулей; все проверки токена ядра
отклоняют отозванные токены (`401 "Token has been revoked"`).

## Безопасность

- **Пароли**: Argon2 (хэширование вне event loop).
- **JWT**: `JWT_ALGORITHM` (HS256/HS384/HS512), ротация через
  `JWT_PREVIOUS_SECRETS` / `JWT_KID`.
- **Refresh-токены**: в БД хранится только SHA-256; семейства ротации.
- **Секреты 2FA**: шифруются ключом `TWOFA_ENCRYPTION_KEY` (список через
  запятую для ротации, первый — актуальный); в prod ключ обязателен — без него
  модуль не стартует.
- **Lockout** (два уровня): на входе 5 неудач за 15 минут блокируют пару
  (аккаунт, IP клиента) — от 30 с до 15 мин, 429; атакующий блокирует только
  себя, владелец с другого адреса входит. Поверх — мягкий per-account порог по
  всем IP: от 20 неудач каждая попытка замедляется (0.5 с за неудачу сверх
  порога, максимум 5 с), но не отклоняется. Компромисс: распределённый
  атакующий получает 5 попыток на IP за окно (плюс rate limiter и задержка), зато
  чужой аккаунт нельзя заблокировать. 2FA/смена пароля (первый фактор уже
  пройден) — жёсткая блокировка на аккаунт. При недоступном кэше в prod — 503.
- **Ротация refresh-токенов**: повтор только что ротированного токена в течение
  10 с при живом преемнике в семействе — безобидная гонка (409, ничего не
  отзывается и не выдаётся); позже или без преемника — reuse, отзыв семейства.
- **Rate limiting** на чувствительных endpoint'ах.
- **Отзыв сессий** при деактивации, сбросе/смене пароля и отзыве роли
  (`revoke_user_tokens_on_commit` ядра после коммита +
  `identity.user_sessions_revoked.v1`).
- **SSO + 2FA**: автопривязка по email к аккаунту с 2FA записывается только
  после успешного второго фактора; неверный binding не «сжигает» state.

## События

Все события — полный конверт `{event_id, event_type, actor, payload}`:

```
identity.user_registered.v1          # {user_id} — без email
identity.user_email_confirmed.v1
identity.user_logged_in.v1           # {user_id, method, provider?}
identity.user_logged_out.v1
identity.user_updated.v1
identity.user_deleted.v1
identity.user_password_reset.v1
identity.user_role_assigned.v1
identity.user_role_revoked.v1
identity.twofa_enabled.v1
identity.twofa_disabled.v1
identity.twofa_backup_codes_regenerated.v1
identity.sso_linked.v1
identity.sso_unlinked.v1
identity.user_sessions_revoked.v1    # {user_id, reason, tokens_valid_after}
```

## Конфигурация

```bash
JWT_SECRET=...                    # в prod — случайный, ≥32 символов
JWT_ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=15
REFRESH_TOKEN_EXPIRE_DAYS=14
TWOFA_ENCRYPTION_KEY=...          # openssl rand -hex 32; обязателен в prod
AUTH_REVOCATION_FAIL_CLOSED=      # по умолчанию true в prod
FRONTEND_URL=http://localhost:3000
SSO_ALLOWED_REDIRECT_URIS=...
GOOGLE_CLIENT_ID=...  GOOGLE_CLIENT_SECRET=...
GITHUB_CLIENT_ID=...  GITHUB_CLIENT_SECRET=...
```

## Миграции

Модуль владеет собственной цепочкой в `identity/migrations/`:

```bash
uv run sandloom migrate upgrade-module identity --apply
```

## Тестирование

```bash
docker compose -f deploy/compose/test.yml up -d --wait
uv run pytest src/sandloom_identity/tests -q
```
