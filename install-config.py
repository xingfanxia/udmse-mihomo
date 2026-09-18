#!/usr/bin/env python3
"""Render YAML scalar values without putting the subscription in argv or logs."""
import ipaddress
import json
import re
from pathlib import Path
import secrets
import sys
from urllib.parse import urlsplit


def validate_dns_ipv4(value: str) -> str:
    address = ipaddress.IPv4Address(value)
    if address.is_unspecified or address.is_multicast or address.is_loopback or address.is_reserved:
        raise ValueError("DNS listener must be a concrete LAN IPv4 address")
    return str(address)


def dns_ipv4_from_routing(text: str) -> str:
    # Accept one literal assignment only. Never source/evaluate the shell file.
    lines = [line for line in text.splitlines()
             if not line.lstrip().startswith("#") and re.search(r"\bDNS_LISTEN_IPV4\b", line)]
    if len(lines) != 1:
        raise ValueError("Routing file requires one literal DNS_LISTEN_IPV4 assignment")
    match = re.fullmatch(
        r"[ \t]*DNS_LISTEN_IPV4=(?:'([^']*)'|\"([^\"]*)\"|([0-9.]+))[ \t]*(?:#.*)?", lines[0]
    )
    if not match:
        raise ValueError("DNS_LISTEN_IPV4 must be a literal IPv4 address")
    return validate_dns_ipv4(next(value for value in match.groups() if value is not None))


def render(template: str, subscription: str, dns_listen_ipv4: str = "192.168.1.1") -> str:
    dns_listen_ipv4 = validate_dns_ipv4(dns_listen_ipv4)
    subscription = subscription.strip()
    try:
        url = urlsplit(subscription)
        valid = url.scheme == "https" and bool(url.hostname) and not any(
            ord(char) < 32 for char in subscription
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Subscription must be a single HTTPS URL")
    replacements = {
        '"YOUR_CLASH_SUBSCRIPTION_URL_HERE"': json.dumps(subscription),
        '"GENERATED_API_SECRET_HERE"': json.dumps(secrets.token_hex(32)),
        '"GENERATED_DNS_LISTEN_HERE"': json.dumps(f"{dns_listen_ipv4}:1053"),
    }
    for placeholder, value in replacements.items():
        if template.count(placeholder) != 1:
            raise ValueError("Configuration template placeholder is missing or duplicated")
        template = template.replace(placeholder, value)
    return template


if __name__ == "__main__":
    try:
        if len(sys.argv) not in (4, 5):
            raise ValueError("Expected template, subscription file, destination, and optional routing file")
        template, subscription_file, destination = map(Path, sys.argv[1:4])
        dns_ipv4 = dns_ipv4_from_routing(Path(sys.argv[4]).read_text()) if len(sys.argv) == 5 else "192.168.1.1"
        rendered = render(template.read_text(), subscription_file.read_text(), dns_ipv4)
        destination.write_text(rendered)
        destination.chmod(0o600)
    except (ValueError, OSError):
        sys.exit("Could not render configuration; check the template, HTTPS subscription file, and literal DNS_LISTEN_IPV4")
