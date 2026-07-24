# SpiritVPN Telegram bot

The Telegram bot is an external adapter. It never receives PostgreSQL or Xray
credentials and talks only to the authenticated backend endpoint:

```text
Telegram -> bot -> POST /internal/v1/vpn/test-access -> backend -> Xray
```

## Staging deployment

Create an untracked mode-`0600` vars file outside the repository:

```yaml
telegram_bot_image: ghcr.io/spirittechdevelopment/spiritvpn-telegram-bot:sha-0000000000000000000000000000000000000000
telegram_bot_token: replace-with-botfather-token
telegram_bot_backend_token: replace-with-backend-service-token
```

If root Docker on control-1 is not already authenticated to the private package,
also provide a scoped read-only GHCR token:

```yaml
telegram_bot_ghcr_username: deployment-service-account
telegram_bot_ghcr_token: replace-with-read-packages-token
```

Deploy only the bot:

```bash
make telegram-bot-staging EXTRA_VARS=/secure/telegram-bot-staging.yml
```

The role validates the immutable image tag, renders a root-only environment
file, starts the non-root container with all Linux capabilities dropped and
verifies `GET http://10.20.0.1:18081/health`.

## Secret boundaries

- Use different BotFather tokens for staging and production.
- Rotate any token pasted into chat, tickets or terminal history.
- The current backend exposes one internal service token. Split it into scoped
  identities before production so bot issuance and Xray snapshot sync can be
  revoked independently.
- Never commit deployment vars or decrypted SOPS material.

## Rollback

Set `telegram_bot_image` to the previous known-good SHA and run the same target.
The bot has no local persistent state, so rollback does not require data
migration.
