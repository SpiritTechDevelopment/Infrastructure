#!/usr/bin/env python3
"""Reconcile Cloudflare A/AAAA records from a desired-state JSON. Stdlib only.

The desired state is derived from the inventory (label -> node -> public IP), so
DNS follows the topology. Token comes from CLOUDFLARE_API_TOKEN (never an arg).

Usage:  cloudflare-dns-sync.py <records.json> [--apply]
        default is a DRY-RUN plan; --apply performs the create/update calls.

records.json:
  {"zone": "example.com",
   "records": [{"name": "entry.example.com", "content": "1.2.3.4",
                "type": "A", "ttl": 1, "proxied": false}]}

NOTE: proxied MUST be false for REALITY (:443) endpoints — Cloudflare's proxy
would terminate/inspect TLS and break the data plane. The role defaults it false.
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.cloudflare.com/client/v4"


def cf(method, path, token, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise SystemExit(f"Cloudflare API {method} {path} -> {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        raise SystemExit(f"Cloudflare API {method} {path} unreachable: {exc}")


def main():
    positional = [a for a in sys.argv[1:] if not a.startswith("-")]
    apply = "--apply" in sys.argv
    if not positional:
        raise SystemExit("usage: cloudflare-dns-sync.py <records.json> [--apply]")

    token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        raise SystemExit("CLOUDFLARE_API_TOKEN is not set")

    with open(positional[0], encoding="utf-8") as handle:
        spec = json.load(handle)
    zone = spec.get("zone", "")
    desired = spec.get("records", [])
    if not zone or not desired:
        print("cloudflare-dns: nothing to reconcile (no zone/records declared)")
        return

    zres = cf("GET", f"/zones?name={zone}", token).get("result") or []
    if not zres:
        raise SystemExit(f"zone not found (token lacks access or wrong name): {zone}")
    zid = zres[0]["id"]

    changes = 0
    for rec in desired:
        name = rec["name"]
        content = rec["content"]
        rtype = rec.get("type", "A")
        ttl = int(rec.get("ttl", 1))
        proxied = bool(rec.get("proxied", False))
        payload = {"type": rtype, "name": name, "content": content,
                   "ttl": ttl, "proxied": proxied}
        cur = cf("GET", f"/zones/{zid}/dns_records?type={rtype}&name={name}",
                 token).get("result") or []
        if not cur:
            print(f"CREATE {name} {rtype} -> {content} (proxied={proxied})")
            changes += 1
            if apply:
                cf("POST", f"/zones/{zid}/dns_records", token, payload)
        else:
            r0 = cur[0]
            drift = (r0["content"] != content
                     or bool(r0["proxied"]) != proxied
                     or int(r0["ttl"]) != ttl)
            if drift:
                print(f"UPDATE {name} {rtype}: {r0['content']} -> {content} "
                      f"(proxied={proxied})")
                changes += 1
                if apply:
                    cf("PUT", f"/zones/{zid}/dns_records/{r0['id']}", token, payload)
            else:
                print(f"ok     {name} {rtype} -> {content}")

    print(f"cloudflare-dns: {'applied' if apply else 'plan'} — {changes} change(s)")


if __name__ == "__main__":
    main()
