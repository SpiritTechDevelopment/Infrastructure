from __future__ import annotations

import json
import re
import subprocess
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment


REPO_ROOT = Path(__file__).resolve().parents[2]


def compiled_node_facts() -> dict[str, object]:
    """The set_fact bodies of roles/compiled_node_plan, merged."""
    tasks = yaml.safe_load(
        (REPO_ROOT / "roles" / "compiled_node_plan" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
    )
    facts: dict[str, object] = {}
    for task in tasks:
        block = task.get("ansible.builtin.set_fact")
        if isinstance(block, dict):
            facts.update(block)
    return facts


def ansible_jinja() -> Environment:
    """Stock Jinja plus the two Ansible filters these templates use."""
    environment = Environment(trim_blocks=True, lstrip_blocks=True)
    environment.filters["to_json"] = json.dumps
    environment.filters["bool"] = lambda value: (
        value
        if isinstance(value, bool)
        else str(value).strip().lower() in ("true", "yes", "on", "1")
    )
    return environment


class SmokeAdapterTests(unittest.TestCase):
    """Every other gate checks state; these check that traffic can move.

    A node can pass all nine remaining gates — containers up, ports listening,
    certificate valid, qdisc applied — and serve nobody.
    """

    PLAN = {
        "instance": {"public_address": "192.0.2.10"},
        "routing": {
            "bridges_as_entry": [
                {"target": {"address": "192.0.2.20", "port": 443}},
                {"target": {"address": "192.0.2.30", "port": 8443}},
            ]
        },
    }

    def render(self, name: str) -> str:
        command = compiled_node_facts()[name][2]
        rendered = ansible_jinja().from_string(command).render(
            spiritvpn_smoke_curl_timeout_seconds=10,
            spiritvpn_smoke_echo_url="https://echo.example.invalid",
            spiritvpn_node_plan=self.PLAN,
        )
        return " ".join(rendered.split())

    def test_exit_must_be_seen_under_its_own_address(self) -> None:
        direct = self.render("spiritvpn_direct_smoke_argv")
        # A proxied or NATed egress answers with a different address, and mere
        # reachability of the internet would not notice.
        self.assertIn('= "192.0.2.10"', direct)
        # Without -4 a dual-stack node answers with its IPv6 address and the
        # comparison fails while the node is perfectly healthy.
        self.assertIn("curl -4", direct)

    def test_every_bridge_is_probed_and_not_just_the_first(self) -> None:
        entry = self.render("spiritvpn_entry_exit_smoke_argv")
        self.assertIn("/dev/tcp/192.0.2.20/443", entry)
        self.assertIn("/dev/tcp/192.0.2.30/8443", entry)

    def test_the_probe_leaves_no_local_proxy_behind(self) -> None:
        # An unauthenticated SOCKS inbound would hand every process on the node
        # — including four host-networked containers — a standing, unaccounted
        # egress through the exit, bought for a two-second check.
        config = (
            REPO_ROOT / "roles" / "xray" / "templates" / "config.json.j2"
        ).read_text(encoding="utf-8")
        defaults = (REPO_ROOT / "roles" / "xray" / "defaults" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("socks", config)
        self.assertNotIn("xray_smoke", defaults)


class XrayAccessLogTests(unittest.TestCase):
    """The access log holds client addresses, destinations and the user.

    Desired state carries `xray.access_log.enabled`, and for a while nothing
    read it: the log path was set unconditionally, so turning the flag off
    changed nothing at all.
    """

    def render_access_log_path(self, *, enabled: bool) -> str:
        expression = compiled_node_facts()["xray_access_log"]
        environment = Environment()
        # Ansible's `bool`, which stock Jinja does not provide.
        environment.filters["bool"] = lambda value: (
            value
            if isinstance(value, bool)
            else str(value).strip().lower() in ("true", "yes", "on", "1")
        )
        return environment.from_string(str(expression)).render(
            spiritvpn_node_plan={
                "infrastructure": {"xray": {"access_log": {"enabled": enabled}}}
            }
        )

    def test_disabled_access_log_is_none_and_never_an_empty_string(self) -> None:
        # An empty string does not disable the log in Xray, it redirects it to
        # stdout — where the json-file driver keeps writing it to disk anyway.
        self.assertEqual(self.render_access_log_path(enabled=False), "none")
        self.assertEqual(
            self.render_access_log_path(enabled=True), "/var/log/xray/access.log"
        )

    def test_role_default_does_not_fall_back_to_stdout(self) -> None:
        defaults = yaml.safe_load(
            (REPO_ROOT / "roles" / "xray" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(defaults["xray_access_log"], "none")

    def test_repository_desired_state_keeps_the_access_log_off(self) -> None:
        common = yaml.safe_load(
            (REPO_ROOT / "desired" / "common" / "xray.yml").read_text(encoding="utf-8")
        )
        self.assertFalse(common["access_log"]["enabled"])
        self.assertFalse(common["access_log"]["export_enabled"])

    def test_export_stays_forbidden_while_enabling_became_a_choice(self) -> None:
        """Only half of the old pair was removed, and deliberately so."""
        semantic = (
            REPO_ROOT / "fleetctl" / "validation" / "semantic.py"
        ).read_text(encoding="utf-8")
        self.assertIn("ACCESS_LOG_EXPORT", semantic)
        self.assertNotIn("ACCESS_LOG_REQUIRED", semantic)

    def test_enabling_the_access_log_still_validates(self) -> None:
        """Turning it on for an investigation must remain possible."""
        from fleetctl.validation import validate_environment

        state = validate_environment(
            REPO_ROOT,
            "develop",
            desired_root=REPO_ROOT / "tests" / "fixtures" / "valid" / "desired",
        )
        self.assertTrue(state.common.xray.access_log_enabled)
        self.assertFalse(state.common.xray.access_log_export_enabled)


class BootstrapReadinessTests(unittest.TestCase):
    def test_readiness_sees_the_expectations_it_checks_against(self) -> None:
        """`include_role` keeps role defaults to itself unless made public.

        readiness.yml compares the node against values the role declares — the
        deployment user among them. Without `public: true` the play dies on an
        undefined variable, and it dies *after* a fully successful bootstrap,
        which makes it look like the node failed rather than the check.
        """
        readiness = yaml.safe_load(
            (REPO_ROOT / "playbooks" / "bootstrap" / "readiness.yml").read_text(
                encoding="utf-8"
            )
        )
        include = next(
            task["ansible.builtin.include_role"]
            for task in readiness[0]["tasks"]
            if "ansible.builtin.include_role" in task
        )
        self.assertTrue(include.get("public"))

        defaults = yaml.safe_load(
            (REPO_ROOT / "roles" / "compiled_node_plan" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        body = (REPO_ROOT / "playbooks" / "bootstrap" / "readiness.yml").read_text(
            encoding="utf-8"
        )
        # At least one role default really is consumed here; if that ever stops
        # being true the assertion above is just ceremony.
        self.assertTrue(any(f"{{{{ {name} }}}}" in body for name in defaults))


class SshPortHandoverTests(unittest.TestCase):
    """Bootstrap must not close the port it is talking over.

    The role rewrites sshd and nftables in the middle of its own run, while
    Ansible holds the session through ControlPersist and may reopen it at any
    moment. Closing the default port there cost two nodes mid-play: exit-ro died
    inside `common`, exit-nl inside `pki_agent`. Handing the port over in
    post_tasks cannot help — the break happens long before they run.
    """

    def render_ports(self, deploy_mode: str, common_ssh_port: object) -> list[int]:
        defaults = yaml.safe_load(
            (REPO_ROOT / "roles" / "common" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        environment = Environment()
        context = {
            "deploy_mode": deploy_mode,
            "common_ssh_port": common_ssh_port,
            "ansible_port": 22,
        }
        context["ssh_port"] = environment.from_string(defaults["ssh_port"]).render(**context).strip()
        rendered = environment.from_string(defaults["common_ssh_ports"]).render(**context)
        return [int(value) for value in re.findall(r"\d+", rendered)]

    def test_bootstrap_keeps_the_default_port_open_alongside_the_declared_one(self) -> None:
        self.assertEqual(self.render_ports("bootstrap", 232), [22, 232])

    def test_steady_state_closes_the_default_port(self) -> None:
        self.assertEqual(self.render_ports("hardened", 232), [232])

    def test_undeclared_port_never_duplicates_the_default(self) -> None:
        # A node that declares nothing must not render `{ 22, 22 }` into nft,
        # which nftables rejects as a duplicate set element.
        self.assertEqual(self.render_ports("bootstrap", ""), [22])

    def test_bootstrap_playbook_no_longer_switches_the_connection(self) -> None:
        playbook = (REPO_ROOT / "playbooks" / "bootstrap" / "bootstrap.yml").read_text(
            encoding="utf-8"
        )
        # readiness.yml runs against the same inventory; moving it to a port the
        # controller has no known_hosts entry for is a different way to fail.
        self.assertNotIn("reset_connection", playbook)
        self.assertNotIn("ansible_port", playbook)


class BridgeCredentialGuardTests(unittest.TestCase):
    """A bridge UUID goes straight into an Xray client id.

    Unlike the REALITY key and the mask PEMs, nothing downstream would notice a
    malformed one: the node comes up, accepts the config and silently refuses to
    route the bridge.
    """

    def compiled_tasks(self) -> list[dict]:
        return yaml.safe_load(
            (REPO_ROOT / "roles" / "compiled_node_plan" / "tasks" / "main.yml").read_text(
                encoding="utf-8"
            )
        )

    def test_bridge_credentials_are_trimmed_where_they_are_used(self) -> None:
        facts = compiled_node_facts()
        for name in ("entry_exits", "xray_static_clients"):
            self.assertIn(
                "service_credential_ref, '') | trim",
                facts[name],
                f"{name} must trim the bridge credential",
            )

    def test_malformed_bridge_credential_fails_the_deployment(self) -> None:
        names = [task.get("name", "") for task in self.compiled_tasks()]
        self.assertIn("Require bridge service credentials to be bare UUIDs", names)
        collect = next(
            task
            for task in self.compiled_tasks()
            if task.get("name") == "Collect bridge service credentials that are not bare UUIDs"
        )
        # The comparison touches secret values, so it must not be echoed; the
        # assertion that reports them names references only.
        self.assertTrue(collect["no_log"])
        report = next(
            task
            for task in self.compiled_tasks()
            if task.get("name") == "Require bridge service credentials to be bare UUIDs"
        )
        self.assertNotIn("no_log", report)

    def test_guard_pattern_accepts_uuids_and_rejects_anything_else(self) -> None:
        collect = next(
            task
            for task in self.compiled_tasks()
            if task.get("name") == "Collect bridge service credentials that are not bare UUIDs"
        )
        body = collect["ansible.builtin.set_fact"]["_spiritvpn_malformed_bridge_credentials"]
        match = re.search(r"'(\^\[0-9a-fA-F\]\{8\}[^']+)'", body)
        self.assertIsNotNone(match, "the UUID pattern must be readable from the task")
        pattern = re.compile(match.group(1))
        self.assertTrue(pattern.match("3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8"))
        self.assertTrue(pattern.match("11111111-2222-4333-8444-555555555555"))
        for rejected in (
            "not-a-uuid",
            "",
            "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8x",
            # The failure this guard exists for, in case trim is ever dropped.
            "3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8\n",
            " 3f2a1b4c-5d6e-4f70-8192-a3b4c5d6e7f8",
        ):
            self.assertIsNone(pattern.match(rejected), rejected)


class BootstrapContourTests(unittest.TestCase):
    def test_bootstrap_and_steady_state_are_separate_playbooks(self) -> None:
        bootstrap = (REPO_ROOT / "playbooks" / "bootstrap" / "bootstrap.yml").read_text(encoding="utf-8")
        configure = (REPO_ROOT / "playbooks" / "deploy" / "configure.yml").read_text(encoding="utf-8")
        self.assertIn("spiritvpn_bootstrap", bootstrap)
        self.assertIn("bootstrap_wireguard", bootstrap)
        self.assertIn("pki_agent", bootstrap)
        self.assertNotIn("compiled_runtime", bootstrap)
        self.assertIn("compiled_runtime", configure)
        self.assertIn("node_agent", configure)
        self.assertNotIn("bootstrap_wireguard", configure)
        wireguard = (REPO_ROOT / "roles" / "bootstrap_wireguard" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Require management hub reachability", wireguard)
        self.assertIn("Reconcile this node as a management-hub peer", wireguard)

    def test_bootstrap_make_target_needs_explicit_apply(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        self.assertIn("fleet-bootstrap: fleet-ansible-check", makefile)
        self.assertIn("refusing bootstrap SSH/mutation: set APPLY=1 explicitly", makefile)
        self.assertIn("no SSH attempted (set CONNECT=1 explicitly)", makefile)

    def test_wireguard_config_is_applied_not_just_written(self) -> None:
        """A rendered config that nothing applies is a config that does nothing.

        `state: started` leaves an already-running wg-quick alone, so a node that
        arrives with a pre-existing wg0 keeps the old address and the old peer —
        while its new public key is already registered on the real hub, which
        then waits for a handshake that never comes.
        """
        tasks = (
            REPO_ROOT / "roles" / "bootstrap_wireguard" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        # Keying off "the file changed" is not enough: on the second run the file
        # is already correct while the kernel still holds the old tunnel, so the
        # decision has to read live state.
        self.assertIn("_bootstrap_wireguard_live_address.stdout", tasks)
        self.assertIn("_bootstrap_wireguard_live_peers.stdout", tasks)
        self.assertIn("'restarted'", tasks)

    def test_hub_reachability_waits_for_the_tunnel_to_converge(self) -> None:
        """One ping right after `wg-quick up` is a race, not a check.

        WireGuard brings the session up lazily on the first packet, and a lost
        first packet is retried only seconds later. entry-ru failed this at
        10:44:25 while its handshake with the hub completed about half a minute
        afterwards — connectivity was fine, the check was simply too early.
        """
        tasks = (
            REPO_ROOT / "roles" / "bootstrap_wireguard" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("until: _bootstrap_wireguard_hub_ping.rc == 0", tasks)
        defaults = yaml.safe_load(
            (REPO_ROOT / "roles" / "bootstrap_wireguard" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        retries = defaults["bootstrap_wireguard_hub_ping_retries"]
        delay = defaults["bootstrap_wireguard_hub_ping_delay_seconds"]
        # Must outlast WireGuard's rekey interval, or the retry buys nothing.
        self.assertGreaterEqual(retries * delay, 25)

    def test_node_local_wireguard_configurator_has_valid_shell(self) -> None:
        path = REPO_ROOT / "roles" / "bootstrap_wireguard" / "templates" / "configure-wireguard.sh.j2"
        rendered = re.sub(r"{{[^\n{}]+}}", "fixture", path.read_text(encoding="utf-8"))
        self.assertNotIn("{{", rendered)
        subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)

    def test_certificate_renewal_hook_has_valid_shell(self) -> None:
        path = REPO_ROOT / "roles" / "pki_agent" / "templates" / "renew-agent-certificate.sh.j2"
        rendered = re.sub(r"{{[^\n{}]+}}", "fixture", path.read_text(encoding="utf-8"))
        self.assertNotIn("{{", rendered)
        subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)

    def test_runtime_reconciliation_does_not_force_container_recreation(self) -> None:
        tasks = (REPO_ROOT / "roles" / "compiled_runtime" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("docker", tasks)
        self.assertIn("compose", tasks)
        self.assertIn("up", tasks)
        self.assertIn("_compiled_runtime_definition.changed", tasks)
        self.assertNotIn("--force-recreate", tasks)
        self.assertNotIn("compose\n      - down", tasks)

    def test_node_agent_uses_pinned_image_persistent_state_and_node_local_pki(self) -> None:
        compose = (
            REPO_ROOT / "roles" / "compiled_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        tasks = (REPO_ROOT / "roles" / "node_agent" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        readiness = (REPO_ROOT / "playbooks" / "operations" / "readiness.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("image: {{ node_agent_image }}", compose)
        self.assertIn("network_mode: host", compose)
        self.assertIn("SPIRIT_STATE_DB_PATH", compose)
        self.assertIn("/var/lib/spirit-agent", compose)
        # Durable users are written with create-temp + rename. A writable file
        # bind cannot support that operation, and Xray must resolve the new
        # inode when its process starts again.
        self.assertIn("SPIRIT_XRAY_CONFIG_PATH: /opt/vpn/xray/config.json", compose)
        self.assertIn("{{ vpn_stack_dir }}/xray:/opt/vpn/xray", compose)
        self.assertIn("{{ vpn_stack_dir }}/xray:/etc/xray:ro", compose)
        self.assertNotIn(
            "{{ vpn_stack_dir }}/xray/config.json:/etc/xray/config.json:ro",
            compose,
        )
        self.assertIn("Grant NodeAgent durable Xray configuration access", tasks)
        self.assertIn('mode: "0770"', tasks)
        self.assertIn('mode: "0660"', tasks)
        self.assertIn("remote_src: true", tasks)
        self.assertIn('node_agent_uid: "65532"', (
            REPO_ROOT / "roles" / "node_agent" / "defaults" / "main.yml"
        ).read_text(encoding="utf-8"))
        self.assertNotIn("ansible.builtin.slurp", tasks)
        # The gate must probe the same overlay address the agent binds. A
        # loopback probe would pass while Prometheus saw nothing.
        #
        # Liveness rather than readiness: readiness additionally means the
        # backend has already reconciled this agent, which happens after these
        # gates and asynchronously. Gating on it would wait on somebody else's
        # work and could never pass on a new node.
        self.assertIn("/health/live", readiness)
        self.assertNotIn("/health/ready", readiness)
        self.assertNotIn("http://127.0.0.1:", readiness)
        self.assertIn(
            "node_agent_http_listen.split(':')[0]\n        == spiritvpn_node_plan.instance.management_address",
            tasks,
        )
        xray_defaults = (REPO_ROOT / "roles" / "xray" / "defaults" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("RoutingService", xray_defaults)


if __name__ == "__main__":
    unittest.main()
