# Authentication

Email/password authentication and Phase 2 ownership authorization are enabled.

## Setup

Configure the existing database settings so `get_test_engine()` connects to
`BFAMS-DeFI-test`. Authentication routes and the bearer-user dependency use
that test engine only.

Set `AUTH_JWT_SECRET` to a randomly generated secret with at least 32 characters.
Without a valid secret, login, refresh, and registration fail closed with HTTP
503, and bearer authentication fails with HTTP 401.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUTH_JWT_SECRET` | unset | HS256 signing secret; at least 32 characters |
| `AUTH_REGISTRATION_ENABLED` | `false` | Set to `true` to enable registration |
| `AUTH_COOKIE_SECURE` | `true` | Controls the refresh cookie's `Secure` flag |

## Endpoints

All routes use the `/auth` prefix.

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/auth/login` | Verify email and password; return a short-lived bearer access token and set a refresh cookie |
| `POST` | `/auth/refresh` | Rotate the refresh token and return a new access token |
| `POST` | `/auth/logout` | Revoke the refresh-token family and clear the refresh cookie |
| `GET` | `/auth/sessions` | List active refresh-token sessions without token material |
| `POST` | `/auth/sessions/{session_id}/revoke` | Revoke one owned session family |
| `POST` | `/auth/logout-all` | Revoke all refresh-token sessions for the authenticated user |
| `POST` | `/auth/change-password` | Verify and replace the password; revoke all refresh sessions |
| `POST` | `/auth/mfa/setup` | Create a pending TOTP authenticator configuration |
| `POST` | `/auth/mfa/verify` | Verify and enable a pending TOTP configuration |
| `GET` | `/auth/mfa/status` | Return whether TOTP MFA is enabled |
| `POST` | `/auth/mfa/disable` | Verify password and TOTP code, then disable MFA |
| `GET` | `/auth/me` | Return the authenticated user's ID and email |
| `POST` | `/auth/register` | Create a user and credential when registration is enabled |

The refresh cookie is named `bfams_refresh`, is HttpOnly, SameSite Strict, scoped
to `/auth`, and lasts seven days. Refresh and logout require the
`X-BFAMS-Auth: 1` header. If an `Origin` header is present, it must match the
request origin.

Login failures are limited per client IP to five per minute and twenty per
hour. Authentication endpoints also enforce account lockout after consecutive
credential failures. Security headers are applied to all responses, including
the interactive API documentation.

## Security notes

Passwords are hashed with Argon2id. Passwords must be 12–128 characters and
cannot equal the normalized email. Login failures use a generic response;
unknown emails use a dummy password hash for timing normalization. Five
consecutive failures lock the credential for 15 minutes.

Access tokens use HS256 and expire after 15 minutes. Refresh tokens are random
opaque values; only their SHA-256 hashes are stored. Refresh-token reuse
revokes the associated token family. Changing a password invalidates access
tokens issued before the recorded password-change time. Authentication audit
events contain only user references, event types, and timestamps; authentication
secrets and credential values are not event fields.

TOTP MFA is optional. When enabled, login requires the password and a six-digit
RFC 6238 code in a 30-second acceptance window. Setup returns the secret and
provisioning URI once; the secret is retained in the `user_totp_secrets` table.
Password changes and logout do not disable MFA.

Authentication is not authorization: this phase intentionally does not attach
the user dependency to unrelated routes.

## Authorization

Private account, connection, transaction, portfolio, dashboard-data, and Setu
routes require a bearer access token. Account and connection lists are filtered
in the database by the authenticated user. Transactions and holdings are
filtered through their owning account and connection. Resource ownership
failures return a generic 404.

Dhan and Setu sync operations validate the account or connection owner before
starting the upstream operation. The existing Dhan local-host, confirmation
header, cooldown, and demo-sync controls remain enabled.

Mutual-fund scheme/NAV analytics operate on shared reference data, and
`GET /api/setu/health`, the static connection prototype, and the dashboard HTML
shell do not return user financial records. The dashboard-data API is private.
No existing non-financial routes are protected by this phase.

## Future wallet-linking architecture

Wallet identities should be added as separate credentials associated with the
same `users` identity, allowing one user to have an email credential and
multiple wallet credentials. Wallet linking and wallet login require a later
phase and do not change the password credential's identity.
