from __future__ import annotations

import json
import os
import re
import subprocess
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

from fleetctl.validation import validate_environment


REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_DESIRED_SKIP_REASON = "encrypted repository desired state requires a trusted SOPS identity"


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
    """Stock Jinja plus the two Ansible filters these templates use.

    StrictUndefined because the alternative is worse than a missing value: a
    default Undefined renders as an empty string, so a template referencing a
    variable the test forgot to pass still produces parseable JSON with an empty
    field, and the assertion below it passes on a config no node would accept.
    """
    environment = Environment(
        trim_blocks=True, lstrip_blocks=True, undefined=StrictUndefined
    )
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

    @unittest.skipIf(os.environ.get("SPIRITVPN_SKIP_LIVE_DESIRED") == "1", LIVE_DESIRED_SKIP_REASON)
    def test_repository_desired_state_keeps_the_access_log_off(self) -> None:
        state = validate_environment(REPO_ROOT, "develop")
        self.assertFalse(state.common.xray.access_log_enabled)
        self.assertFalse(state.common.xray.access_log_export_enabled)

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


class XrayApiRuntimeTests(unittest.TestCase):
    """The API must be a real readiness dependency, not a syntax-only claim."""

    def test_api_uses_direct_loopback_listener_without_recursive_tunnel(self) -> None:
        config = (
            REPO_ROOT / "roles" / "xray" / "templates" / "config.json.j2"
        ).read_text(encoding="utf-8")
        self.assertIn('"listen": "{{ xray_api_bind }}:{{ xray_api_port }}"', config)
        self.assertNotIn('"protocol": "tunnel"', config)
        self.assertNotIn('"inboundTag": ["api"]', config)
        self.assertNotIn('"outboundTag": "api"', config)

    def test_healthcheck_calls_the_live_stats_api(self) -> None:
        compose = (
            REPO_ROOT / "roles" / "compiled_runtime" / "templates" / "compose.yml.j2"
        ).read_text(encoding="utf-8")
        self.assertIn(
            'test: ["CMD", "xray", "api", "statsquery", '
            '"--server=127.0.0.1:10085"]',
            compose,
        )
        self.assertNotIn(
            'test: ["CMD", "xray", "run", "-test", '
            '"-config", "/etc/xray/config.json"]',
            compose,
        )

    def test_changed_startup_config_restarts_xray_and_waits_for_api(self) -> None:
        tasks = (
            REPO_ROOT / "roles" / "compiled_runtime" / "tasks" / "main.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("Restart Xray onto a changed startup configuration", tasks)
        self.assertIn("Wait for the restarted Xray API", tasks)
        self.assertGreaterEqual(tasks.count("_xray_config_render.changed"), 2)
        restart = tasks.index("Restart Xray onto a changed startup configuration")
        wait = tasks.index("Wait for the restarted Xray API")
        self.assertLess(restart, wait)
        self.assertIn("--wait-timeout", tasks[wait:])


class XrayBridgeRoutingTests(unittest.TestCase):
    """An exit must allow its static entry bridge before agent default-deny."""

    @staticmethod
    def xray_defaults() -> dict:
        return yaml.safe_load(
            (REPO_ROOT / "roles" / "xray" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )

    def render_exit(self) -> dict:
        template = ansible_jinja().from_string(
            (REPO_ROOT / "roles" / "xray" / "templates" / "config.json.j2").read_text(
                encoding="utf-8"
            )
        )
        rendered = template.render(
            xray_loglevel="warning",
            xray_access_log="none",
            xray_error_log="/var/log/xray/error.log",
            xray_mask_address="quarter",
            xray_dns_servers=[],
            xray_enable_api=True,
            xray_api_bind="127.0.0.1",
            xray_api_port=10085,
            xray_api_services=["HandlerService", "RoutingService", "StatsService"],
            xray_metrics_enabled=False,
            xray_stats_user_traffic=True,
            xray_listen_port=443,
            xray_transport="tcp",
            xray_xhttp_path="",
            xray_xhttp_mode="auto",
            xray_inbound_tag="xi-vless",
            xray_static_clients=[
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "email": "bridge-develop-entry-ru.to-develop-exit-ro",
                    "flow": "xtls-rprx-vision",
                }
            ],
            reality_dest="www.example.com:443",
            reality_server_names=["ro.example.com"],
            reality_private_key_eff="private-key",
            reality_short_ids=["0123456789abcdef"],
            xray_sniffing_route_only=True,
            node_role="exit",
            entry_exits=[],
            xray_direct_outbound_tag="direct",
            xray_block_outbound_tag="block",
            xray_domain_strategy="AsIs",
            xray_block_domains=[],
            xray_manage_routing_via_agent=True,
            entry_default_exit_tag="",
            xray_entry_block_unmatched=True,
            xray_private_networks=self.xray_defaults()["xray_private_networks"],
        )
        return json.loads(rendered)

    def test_exit_static_bridge_clients_route_to_freedom(self) -> None:
        config = self.render_exit()

        bridge_rule = next(
            rule
            for rule in config["routing"]["rules"]
            if rule.get("ruleTag")
            == "spirit-static:bridge:bridge-develop-entry-ru.to-develop-exit-ro"
        )
        self.assertEqual(
            bridge_rule,
            {
                "type": "field",
                "user": ["bridge-develop-entry-ru.to-develop-exit-ro"],
                "outboundTag": "direct",
                "ruleTag": "spirit-static:bridge:bridge-develop-entry-ru.to-develop-exit-ro",
            },
        )

    def test_private_networks_are_blocked_first_without_geo_assets(self) -> None:
        config = self.render_exit()
        first = config["routing"]["rules"][0]
        self.assertEqual(first["outboundTag"], "block")
        for network in ("10.0.0.0/8", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12"):
            self.assertIn(network, first["ip"])
        for entry in first["ip"]:
            self.assertFalse(entry.startswith(("geoip:", "geosite:")), entry)


class XrayTransportTests(unittest.TestCase):
    """XHTTP and TCP share one template, and the differences are not cosmetic.

    Xray rejects `xtls-rprx-vision` over XHTTP with "XTLS only supports TLS and
    REALITY directly for now.", and it does so on the client, before the node is
    ever contacted: a node rendered with a flow it must not have accepts nobody
    and reports nothing. `xray run -test` does not catch it either — the config
    is valid, just unusable.
    """

    @staticmethod
    def xray_defaults() -> dict:
        return yaml.safe_load(
            (REPO_ROOT / "roles" / "xray" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )

    def render(self, **overrides: object) -> dict:
        template = ansible_jinja().from_string(
            (REPO_ROOT / "roles" / "xray" / "templates" / "config.json.j2").read_text(
                encoding="utf-8"
            )
        )
        context: dict[str, object] = dict(
            xray_loglevel="warning",
            xray_access_log="none",
            xray_error_log="/var/log/xray/error.log",
            xray_mask_address="quarter",
            xray_dns_servers=[],
            xray_enable_api=True,
            xray_api_bind="127.0.0.1",
            xray_api_port=10085,
            xray_api_services=["HandlerService"],
            xray_metrics_enabled=False,
            xray_stats_user_traffic=True,
            xray_listen_port=443,
            xray_transport="tcp",
            xray_xhttp_path="",
            xray_xhttp_mode="auto",
            xray_inbound_tag="xi-vless",
            xray_static_clients=[],
            reality_dest="127.0.0.1:8443",
            reality_server_names=["vmshare.example.invalid"],
            reality_private_key_eff="k" * 43,
            reality_short_ids=["8456802426f0b3c1"],
            xray_sniffing_route_only=True,
            node_role="entry",
            entry_exits=[],
            xray_direct_outbound_tag="direct",
            xray_block_outbound_tag="block",
            xray_domain_strategy="AsIs",
            xray_block_domains=[],
            xray_manage_routing_via_agent=True,
            entry_default_exit_tag="",
            xray_entry_block_unmatched=True,
            xray_private_networks=self.xray_defaults()["xray_private_networks"],
        )
        context.update(overrides)
        return json.loads(template.render(**context))

    @staticmethod
    def exit_outbound(*, transport: str) -> dict:
        common = {
            "tag": f"xo-{transport}",
            "address": "192.0.2.30",
            "port": 443,
            "uuid": "22222222-2222-4222-8222-222222222222",
            "reality_sni": "exit.example.invalid",
            "reality_password": "public-key",
            "reality_short_id": "ab",
            "fingerprint": "chrome",
        }
        if transport == "xhttp":
            return {
                **common,
                "transport": "xhttp",
                "flow": "",
                "xhttp_path": "/stat/",
                "xhttp_mode": "packet-up",
            }
        return {
            **common,
            "transport": "tcp",
            "flow": "xtls-rprx-vision",
            "xhttp_path": "",
            "xhttp_mode": "",
        }

    def test_tcp_inbound_keeps_its_shape(self) -> None:
        stream = self.render(
            xray_static_clients=[{"id": "i", "email": "bridge-x", "flow": "xtls-rprx-vision"}],
        )["inbounds"][0]
        self.assertEqual(stream["streamSettings"]["network"], "tcp")
        self.assertNotIn("xhttpSettings", stream["streamSettings"])
        self.assertEqual(stream["settings"]["clients"][0]["flow"], "xtls-rprx-vision")

    def test_xhttp_inbound_carries_path_and_drops_flow(self) -> None:
        stream = self.render(
            xray_transport="xhttp",
            xray_xhttp_path="/stat/",
            xray_xhttp_mode="auto",
            xray_static_clients=[{"id": "i", "email": "bridge-x", "flow": ""}],
        )["inbounds"][0]

        self.assertEqual(stream["streamSettings"]["network"], "xhttp")
        self.assertEqual(
            stream["streamSettings"]["xhttpSettings"], {"path": "/stat/", "mode": "auto"}
        )
        self.assertEqual(stream["streamSettings"]["security"], "reality")
        self.assertNotIn("flow", stream["settings"]["clients"][0])

    def test_inbound_mode_stays_permissive(self) -> None:
        """Server `auto` accepts any client mode; `packet-up` would refuse the
        `stream-one` that a client on `auto` picks under REALITY."""

        self.assertEqual(self.xray_defaults()["xray_xhttp_mode"], "auto")

    def test_each_bridge_outbound_follows_its_own_exit(self) -> None:
        config = self.render(
            xray_transport="xhttp",
            xray_xhttp_path="/stat/",
            entry_exits=[
                self.exit_outbound(transport="tcp"),
                self.exit_outbound(transport="xhttp"),
            ],
        )
        outbounds = {
            item["tag"]: item for item in config["outbounds"] if item.get("protocol") == "vless"
        }

        tcp = outbounds["xo-tcp"]["streamSettings"]
        self.assertEqual(tcp["network"], "tcp")
        self.assertNotIn("xhttpSettings", tcp)
        self.assertEqual(
            outbounds["xo-tcp"]["settings"]["vnext"][0]["users"][0]["flow"],
            "xtls-rprx-vision",
        )

        xhttp = outbounds["xo-xhttp"]["streamSettings"]
        self.assertEqual(xhttp["network"], "xhttp")
        self.assertEqual(xhttp["xhttpSettings"], {"path": "/stat/", "mode": "packet-up"})
        self.assertNotIn("flow", outbounds["xo-xhttp"]["settings"]["vnext"][0]["users"][0])

    def test_role_refuses_a_transport_that_disagrees_with_the_path(self) -> None:
        tasks = (REPO_ROOT / "roles" / "xray" / "tasks" / "main.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("xray_transport in ['tcp', 'xhttp']", tasks)
        self.assertIn("xray_transport != 'xhttp' or (xray_xhttp_path | length) > 0", tasks)
        self.assertIn("xray_transport != 'tcp' or (xray_xhttp_path | length) == 0", tasks)


class SshPortHandoverTests(unittest.TestCase):
    """Bootstrap must not close the port it is talking over.

    The role rewrites sshd and nftables in the middle of its own run, while
    Ansible holds the session through ControlPersist and may reopen it at any
    moment. Closing the default port there cost two nodes mid-play: exit-ro died
    inside `common`, exit-nl inside `pki_agent`. Handing the port over in
    post_tasks cannot help — the break happens long before they run.
    """

    def render_ports(
        self,
        deploy_mode: str,
        common_ssh_port: object,
        *,
        ansible_port: int = 22,
    ) -> list[int]:
        defaults = yaml.safe_load(
            (REPO_ROOT / "roles" / "common" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        environment = Environment()
        context = {
            "deploy_mode": deploy_mode,
            "common_ssh_port": common_ssh_port,
            "ansible_port": ansible_port,
        }
        context["ssh_port"] = environment.from_string(defaults["ssh_port"]).render(**context).strip()
        rendered = environment.from_string(defaults["common_ssh_ports"]).render(**context)
        return [int(value) for value in re.findall(r"\d+", rendered)]

    def test_bootstrap_keeps_the_default_port_open_alongside_the_declared_one(self) -> None:
        self.assertEqual(self.render_ports("bootstrap", 232), [22, 232])

    def test_bootstrap_keeps_a_non_default_connection_port_open(self) -> None:
        self.assertEqual(
            self.render_ports("bootstrap", 232, ansible_port=2222),
            [2222, 232],
        )

    def test_bootstrap_deduplicates_matching_connection_and_declared_ports(self) -> None:
        self.assertEqual(
            self.render_ports("bootstrap", 232, ansible_port=232),
            [232],
        )

    def test_steady_state_closes_the_default_port(self) -> None:
        self.assertEqual(self.render_ports("hardened", 232), [232])

    def test_steady_state_restricts_ssh_to_the_management_overlay(self) -> None:
        mapping = compiled_node_facts()
        expression = mapping["ssh_allowed_cidrs"]
        environment = Environment()
        bootstrap = environment.from_string(expression).render(
            spiritvpn_deploy_mode="bootstrap",
            spiritvpn_node_plan={"instance": {"management_network": "10.80.0.0/16"}},
        )
        hardened = environment.from_string(expression).render(
            spiritvpn_deploy_mode="hardened",
            spiritvpn_node_plan={"instance": {"management_network": "10.80.0.0/16"}},
        )
        self.assertEqual(yaml.safe_load(bootstrap), [])
        self.assertEqual(yaml.safe_load(hardened), ["10.80.0.0/16"])

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

    def test_known_bootstrap_escape_hatch_is_retired_in_steady_state(self) -> None:
        defaults = yaml.safe_load(
            (REPO_ROOT / "roles" / "common" / "defaults" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        tasks = yaml.safe_load(
            (REPO_ROOT / "roles" / "common" / "tasks" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("99-temp-bootstrap.conf", defaults["common_retired_sshd_dropins"])
        removal = next(
            task for task in tasks if task.get("name") == "Remove explicitly retired sshd drop-ins"
        )
        self.assertEqual(removal["ansible.builtin.file"]["state"], "absent")

    def test_live_sshd_is_reconciled_even_when_the_file_did_not_change(self) -> None:
        """An interrupted bootstrap may write the drop-in without running handlers."""
        tasks = yaml.safe_load(
            (REPO_ROOT / "roles" / "common" / "tasks" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        names = [task.get("name") for task in tasks]
        validate_name = "Validate the assembled sshd configuration on every hardened run"
        reload_name = "Reconcile the live sshd configuration on every hardened run"
        listen_name = "Require sshd to listen on every declared port"
        validate = tasks[names.index(validate_name)]
        reload = tasks[names.index(reload_name)]
        listener = tasks[names.index(listen_name)]

        self.assertEqual(validate["ansible.builtin.command"]["argv"], ["/usr/sbin/sshd", "-t"])
        self.assertFalse(validate["changed_when"])
        self.assertEqual(reload["ansible.builtin.service"]["state"], "reloaded")
        self.assertFalse(reload["changed_when"])
        self.assertEqual(listener["loop"], "{{ common_ssh_ports }}")
        self.assertEqual(listener["ansible.builtin.wait_for"]["state"], "started")
        self.assertLess(
            names.index("Remove explicitly retired sshd drop-ins"),
            names.index(validate_name),
        )
        self.assertLess(names.index(validate_name), names.index(reload_name))
        self.assertLess(names.index(reload_name), names.index(listen_name))

    def test_live_nftables_is_reconciled_even_when_the_file_did_not_change(self) -> None:
        tasks = yaml.safe_load(
            (REPO_ROOT / "roles" / "common" / "tasks" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        reconcile = next(
            task
            for task in tasks
            if task.get("name") == "Reconcile the live nftables ruleset on every hardened run"
        )
        self.assertEqual(reconcile["ansible.builtin.command"], "nft -f /etc/nftables.conf")
        self.assertFalse(reconcile["changed_when"])
        service = next(task for task in tasks if task.get("name") == "Start and enable nftables service")
        self.assertEqual(service["ansible.builtin.service"]["state"], "started")
        self.assertTrue(service["ansible.builtin.service"]["enabled"])

    def test_managed_firewall_retires_competing_input_owners(self) -> None:
        tasks = yaml.safe_load(
            (REPO_ROOT / "roles" / "common" / "tasks" / "main.yml").read_text(
                encoding="utf-8"
            )
        )
        by_name = {task.get("name"): task for task in tasks}

        install = by_name["Install nftables firewall tooling"]["ansible.builtin.apt"]
        self.assertEqual(install["name"], ["nftables", "iptables"])

        retire = by_name["Remove competing persistent firewall managers"][
            "ansible.builtin.apt"
        ]
        self.assertEqual(
            retire["name"],
            ["ufw", "iptables-persistent", "netfilter-persistent"],
        )
        self.assertEqual(retire["state"], "absent")
        self.assertTrue(retire["purge"])

        expected_commands = {
            "Set the legacy IPv4 INPUT policy to accept": [
                "iptables",
                "-w",
                "-P",
                "INPUT",
                "ACCEPT",
            ],
            "Remove rules from the legacy IPv4 INPUT chain": [
                "iptables",
                "-w",
                "-F",
                "INPUT",
            ],
            "Set the legacy IPv6 INPUT policy to accept": [
                "ip6tables",
                "-w",
                "-P",
                "INPUT",
                "ACCEPT",
            ],
            "Remove rules from the legacy IPv6 INPUT chain": [
                "ip6tables",
                "-w",
                "-F",
                "INPUT",
            ],
        }
        for name, argv in expected_commands.items():
            with self.subTest(task=name):
                self.assertEqual(by_name[name]["ansible.builtin.command"]["argv"], argv)
                self.assertIn("not ansible_check_mode", by_name[name]["when"])

        names = [task.get("name") for task in tasks]
        self.assertLess(
            names.index("Reconcile the live nftables ruleset on every hardened run"),
            names.index("Remove competing persistent firewall managers"),
        )

    def test_fail2ban_uses_the_managed_nftables_backend(self) -> None:
        jail = (REPO_ROOT / "roles" / "common" / "templates" / "jail.local.j2").read_text(
            encoding="utf-8"
        )
        self.assertIn("banaction = nftables-multiport", jail)
        self.assertIn("banaction_allports = nftables-allports", jail)

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
