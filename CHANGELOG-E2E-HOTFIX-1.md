# E2E hotfix 1 — controller-local privilege and TLS/SNI consistency

## Failure fixed

`make deploy-e2e` stopped during offline rendering with:

```text
sudo: a password is required
TASK [Ensure secure render directory] FAILED
```

The static inventory set `ansible_become: true` globally. The render play used
`connection: local`, so the remote-host variable leaked onto the workstation and
Ansible attempted local `sudo` before contacting any server.

## Changes

- Global privilege escalation now defaults to off.
- Shipped inventories no longer set `ansible_become` in `all.vars`.
- Every controller-only play explicitly uses:

  ```yaml
  connection: local
  become: false
  vars:
    ansible_become: false
  ```

- Remote deployment/verification plays still explicitly use `become: true`.
- The render wrapper passes `ansible_become=false` as an extra variable, so a
  caller's inventory cannot accidentally force local sudo.
- Added a static-check-only inventory fixture containing the dynamic group names;
  syntax validation no longer emits misleading unmatched-host warnings.
- Corrected production exit REALITY names from `www.fr.vmshare.ru` /
  `www.ru.vmshare.ru` /
  `www.nl.vmshare.ru` to `fr.vmshare.ru` / `ru.vmshare.ru` /
  `nl.vmshare.ru`. A `*.vmshare.ru` certificate does not cover two-label names
  such as `www.fr.vmshare.ru`.
- Named every imported play in `site.yml` and fixed the SOPS YAML line wrapping.

## Validation completed

- `make lint`: passed, zero warnings.
- `make check`: passed with Ansible Core 2.18.18.
- `make test-api-wrapper`: passed.
- `make check` as an unprivileged local user with no sudo: passed.
- Production inventory preflight with a generated `vmshare.ru` + `*.vmshare.ru`
  test certificate: passed for one platform, one entry, and two enabled exits.
- Rendered entry and exit JSON parsed successfully.

The Xray binary/container deep config test was not run in the packaging environment
because Docker/Xray was unavailable there. `make deploy-e2e` will run that test on
the operator workstation before touching the servers when Docker is available.
