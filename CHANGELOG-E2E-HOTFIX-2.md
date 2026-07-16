# E2E hotfix 2

This release fixes the first live deployment failure after local rendering succeeded.

## Root cause

The production inventory forced `~/.ssh/id_ed25519`, while `ansible.cfg` forced
`PreferredAuthentications=publickey`. Two enabled hosts did not accept that key, and the
deployment had no supported password mode. Ansible then continued into later imported
plays, producing a secondary hidden wiring error because REALITY readback facts were absent.

## Changes

- Removed the hard-coded SSH private-key path from runtime and example inventories.
- Removed the global public-key-only SSH preference.
- Added `SSH_AUTH=auto|key|password` and `SSH_KEY=...` to every remote Make target.
- Password mode disables public-key attempts, avoiding `MaxAuthTries`, and prompts once.
- Added a non-mutating all-host SSH/Python gate before any deployment mutation.
- Added completion gates after platform, exit, REALITY readback, and entry phases.
- Replaced the censored missing-fact wiring failure with explicit failed-host reporting.
- Kept all access hardening, nftables, Fail2ban, and WireGuard deployment disabled.
