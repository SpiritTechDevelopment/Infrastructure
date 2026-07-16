# E2E hotfix 4 — REALITY key parser

The live exit deployment reached Xray key generation but failed before storing the
new key. The command itself succeeded. The Ansible parser was wrong: regular
expressions inside Jinja block expressions used doubled backslashes (`\\s`). In
this context Jinja passed a literal backslash to the regex engine, so valid output
such as `PrivateKey: ...` never matched.

Changes:

- Parse Xray key output from both `stdout` and `stderr`.
- Use character classes instead of fragile backslash escapes.
- Support current `PrivateKey` / `Password (PublicKey)` output, the earlier
  `Password` label, and legacy `Private key` / `Public key` output.
- Make the SSH fallback client generator capture both output streams.
- Add a local Ansible regression test covering modern, stderr-only, and legacy
  output formats; `make check` now runs it before deployment.

The failed run generated an ephemeral key but did not persist it. Re-running the
full deployment is safe: platform tasks are idempotent, and the exit will generate
and store a fresh key before rendering or starting Xray.
