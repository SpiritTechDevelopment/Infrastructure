# Local-controller privilege escalation fix

The initial E2E package inherited `ansible_become: true` from `all.vars` while
rendering configs with `connection: local`. On a normal workstation this made
Ansible run local `file`/`template` modules through `sudo`, so `make check`
failed before contacting any server.

This patch:

- removes global `ansible_become: true` from shipped inventories;
- changes the Ansible default to `become = False`;
- keeps `become: true` only on plays that operate on remote servers;
- sets `connection: local`, `become: false`, and `ansible_become: false` on all
  controller-only plays;
- passes `ansible_become=false` explicitly in the render-check wrapper.

Static validation now runs entirely as the invoking workstation user and does
not require a sudo password.
