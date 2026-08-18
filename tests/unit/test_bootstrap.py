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
    environment = Environment()
    environment.filters["to_json"] = json.dumps
    environment.filters["bool"] = lambda value: (
        value
        if isinstance(value, bool)
        else str(value).strip().lower() in ("true", "yes", "on", "1")
    )
    return environment


class SmokeAdapterTests(unittest.TestCase):
    """The readiness gates are fail-closed, and an adapter that only proves the
    node has internet would pass on a node whose tunnel is dead."""

    XRAY_VARIABLES = {
        "xray_loglevel": "info",
        "xray_access_log": "none",
        "xray_error_log": "/var/log/xray/error.log",
        "xray_mask_address": "",
        "xray_dns_servers": [],
        "xray_enable_api": True,
        "xray_api_services": ["HandlerService"],
        "xray_api_bind": "127.0.0.1",
        "xray_api_port": 10085,
        "xray_metrics_enabled": True,
        "xray_metrics_bind": "127.0.0.1",
        "xray_metrics_port": 11111,
        "xray_stats_user_traffic": "true",
        "xray_listen_port": 443,
        "xray_inbound_tag": "xi-vless",
        "xray_static_clients": [],
        "reality_dest": "127.0.0.1:8443",
        "reality_server_names": ["node.example.invalid"],
        "reality_private_key_eff": "k" * 43,
        "reality_short_ids": ["8456802426f0b3c1"],
        "xray_sniffing_route_only": True,
        "xray_direct_outbound_tag": "direct",
        "xray_block_outbound_tag": "block",
        "xray_domain_strategy": "AsIs",
        "xray_block_domains": [],
        "xray_manage_routing_via_agent": True,
        "entry_default_exit_tag": "",
        "xray_entry_block_unmatched": True,
        "xray_smoke_inbound_tag": "xi-smoke",
        "xray_smoke_socks_port": 10808,
    }
    EXITS = [
        {
            "tag": "xo-example-exit",
            "address": "192.0.2.20",
            "port": 443,
            "uuid": "u",
            "reality_sni": "exit.example.invalid",
            "reality_password": "p",
            "reality_short_id": "294753b602c08325",
            "fingerprint": "chrome",
            "flow": "xtls-rprx-vision",
            "email": "bridge-x",
        }
    ]
    PLAN = {
        "instance": {"public_address": "192.0.2.10"},
        "routing": {"bridges_as_entry": [{"target": {"address": "192.0.2.20"}}]},
    }

    def render_config(self, **overrides: object) -> dict:
        source = (
            REPO_ROOT / "roles" / "xray" / "templates" / "config.json.j2"
        ).read_text(encoding="utf-8")
        rendered = ansible_jinja().from_string(source).render(
            **self.XRAY_VARIABLES, **overrides
        )
        # A conditional block that breaks a comma produces a file Xray refuses
        # to load, and the deployment would only find out on the node.
        return json.loads(rendered)

    def test_smoke_inbound_exists_only_where_it_has_somewhere_to_go(self) -> None:
        exit_side = self.render_config(
            node_role="exit", entry_exits=[], xray_smoke_inbound_enabled=False
        )
        self.assertNotIn("xi-smoke", [item["tag"] for item in exit_side["inbounds"]])

        entry_side = self.render_config(
            node_role="entry", entry_exits=self.EXITS, xray_smoke_inbound_enabled=True
        )
        smoke = next(
            item for item in entry_side["inbounds"] if item["tag"] == "xi-smoke"
        )
        # Reachable from the node and from nowhere else.
        self.assertEqual(smoke["listen"], "127.0.0.1")
        self.assertEqual(smoke["protocol"], "socks")

        rules = [
            rule
            for rule in entry_side["routing"]["rules"]
            if rule.get("inboundTag") == ["xi-smoke"]
        ]
        # The point of the test is the route, so the smoke traffic must leave
        # through the very outbound customer traffic uses.
        self.assertEqual([rule["outboundTag"] for rule in rules], ["xo-example-exit"])

    def test_adapters_compare_the_visible_address_with_the_expected_one(self) -> None:
        facts = compiled_node_facts()
        environment = ansible_jinja()
        common = {
            "spiritvpn_smoke_curl_timeout_seconds": 10,
            "spiritvpn_smoke_echo_url": "https://echo.example.invalid",
            "spiritvpn_smoke_socks_port": 10808,
            "spiritvpn_node_plan": self.PLAN,
        }
        direct = environment.from_string(
            facts["spiritvpn_direct_smoke_argv"][2]
        ).render(**common)
        # An exit must be seen under its own address: a proxied or NATed egress
        # answers with a different one.
        self.assertIn('= "192.0.2.10"', direct)

        entry = environment.from_string(
            facts["spiritvpn_entry_exit_smoke_argv"][2]
        ).render(**common)
        self.assertIn("--socks5-hostname 127.0.0.1:10808", entry)
        # And an entry must be seen under the exit's address, never its own —
        # that difference is the whole proof that the bridge carries traffic.
        self.assertIn('= "192.0.2.20"', entry)
        self.assertNotIn("192.0.2.10", entry)


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
