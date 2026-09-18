"""Local installer checks. All router paths and commands use a temporary fixture."""
import gzip
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]
BASH = "/opt/homebrew/bin/bash" if Path("/opt/homebrew/bin/bash").exists() else shutil.which("bash")
spec = importlib.util.spec_from_file_location("install_config", REPO / "install-config.py")
config = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config)


class RenderTest(unittest.TestCase):
    def test_url_escaping_and_random_secret(self):
        url = 'https://example.invalid/sub?a=1&b="quote"|\\path#fragment'
        template = (REPO / "config.yaml").read_text()
        first = config.render(template, url)
        second = config.render(template, url)
        line = next(line for line in first.splitlines() if line.strip().startswith("url:"))
        self.assertEqual(json.loads(line.split(":", 1)[1]), url)
        self.assertNotEqual(first, second)
        self.assertNotIn("GENERATED_API_SECRET_HERE", first)

    def test_dns_listener_rendering(self):
        rendered = config.render((REPO / "config.yaml").read_text(), "https://example.invalid/sub", "192.168.20.1")
        line = next(line for line in rendered.splitlines() if line.strip().startswith("listen:"))
        self.assertEqual(json.loads(line.split(":", 1)[1]), "192.168.20.1:1053")
        for value in ["0.0.0.0", "::", "::1", "hostname", "127.0.0.1", "224.0.0.1", "192.168.1.1:1053"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.render((REPO / "config.yaml").read_text(), "https://example.invalid/sub", value)

    def test_dns_routing_literal_parser(self):
        for assignment in ["DNS_LISTEN_IPV4='192.168.20.1'", 'DNS_LISTEN_IPV4="192.168.20.1" # LAN', "DNS_LISTEN_IPV4=192.168.20.1"]:
            self.assertEqual(config.dns_ipv4_from_routing(assignment), "192.168.20.1")
        for assignment in ["", "DNS_LISTEN_IPV4=$(echo 192.168.20.1)", 'DNS_LISTEN_IPV4="$LAN_IP"', "DNS_LISTEN_IPV4=192.168.1.1\nDNS_LISTEN_IPV4=192.168.2.1", "export DNS_LISTEN_IPV4=192.168.1.1"]:
            with self.subTest(assignment=assignment), self.assertRaises(ValueError):
                config.dns_ipv4_from_routing(assignment)

    def test_reject_insecure_or_multiline_subscription(self):
        for url in ["http://example.invalid/x", "https://", "https://example.invalid/\nsecret"]:
            with self.assertRaises(ValueError):
                config.render((REPO / "config.yaml").read_text(), url)


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "checkout"
        self.source.mkdir()
        self.data = self.root / "data"
        self.data.mkdir()
        self.units = self.root / "units"
        self.units.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands"
        self.subscription = self.root / "subscription"
        self.subscription.write_text('https://example.invalid/private?token=A&B="C"')
        self.archive = self.root / "mihomo.gz"
        self.archive.write_bytes(gzip.compress(b'#!/bin/bash\nexit "${CONFIG_FAILURE:-0}"\n'))
        script = (REPO / "install.sh").read_text()
        script = script.replace("/data", str(self.data)).replace("/etc/systemd/system", str(self.units))
        script = script.replace("/run/lock/udmse-mihomo-install.lock", str(self.root / "install.lock"))
        script = script.replace("/run/mihomo-routing", str(self.root / "routing-state"))
        for original in ("/run/systemd/system", "/lib/systemd/system", "/usr/lib/systemd/system"):
            script = script.replace(original, str(self.root / original.lstrip("/")))
        script = script.replace("9e0f11afbf38426b8bd88fdc594678f8161c57eccb4e1b77acb12b493904f1d4", hashlib.sha256(self.archive.read_bytes()).hexdigest())
        (self.source / "install.sh").write_text(script)
        for name in ("config.yaml", "routing.env.example", "install-config.py", "uninstall.sh"):
            shutil.copyfile(REPO / name, self.source / name)
        for name in ("mihomo-routing.sh", "20-mihomo.sh", "mihomo-watchdog.sh", "mihomo.service", "mihomo-watchdog.service", "mihomo-watchdog.timer", "mihomo-admin.service", "admin-server.py", "telemetry.py"):
            (self.source / name).write_text("# fixture\n")
        (self.source / "admin").mkdir()
        for name in ("index.html", "app.js", "style.css", "favicon.svg", "telemetry.js"):
            (self.source / "admin" / name).write_text("fixture")
        self.mock("sha256sum", 'exec "$CHECKSUM_COMMAND" "$@"')
        self.mock("id", "echo 0")
        self.mock("uname", 'if [[ $1 == -s ]]; then echo Linux; else echo aarch64; fi')
        for command in ("flock", "ip", "iptables", "ip6tables"):
            self.mock(command, "exit 0")
        self.mock("systemctl", 'echo "$*" >> "$COMMAND_LOG"\nexit "${RELOAD_FAILURE:-0}"')
        self.mock("curl", '''[[ ${DOWNLOAD_FAILURE:-0} == 0 ]] || exit 22
while (($#)); do
  if [[ $1 == --output ]]; then output=$2; shift 2; else url=$1; shift; fi
done
if [[ $url == *.gz ]]; then cp "$ARCHIVE" "$output"; else printf 'MRS fixture' > "$output"; fi
if [[ ${CORRUPT_DOWNLOAD:-0} == 1 ]]; then printf broken >> "$output"; fi''')
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"], COMMAND_LOG=str(self.log), ARCHIVE=str(self.archive), CHECKSUM_COMMAND=shutil.which("gsha256sum") or shutil.which("sha256sum"))

    def tearDown(self):
        self.temp.cleanup()

    def mock(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -eu\n" + body + "\n")
        path.chmod(0o700)

    def run_install(self, routing_file=None, **env):
        arguments = [BASH, str(self.source / "install.sh"), "--subscription-file", str(self.subscription)]
        if routing_file is not None:
            arguments += ["--routing-file", str(routing_file)]
        return subprocess.run(arguments, env=dict(self.env, **env), capture_output=True, text=True)

    def assert_clean_failure(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((self.data / "mihomo").exists())
        self.assertEqual(list(self.data.glob(".mihomo-install.*")), [])
        self.assertEqual(list(self.units.iterdir()), [])
        self.assertNotIn("private?token", result.stdout + result.stderr)

    def test_supplied_routing_file_controls_dns_listener(self):
        routing = self.root / "custom-routing.env"
        routing.write_text((REPO / "routing.env.example").read_text().replace("192.168.1.1", "192.168.20.1"))
        result = self.run_install(routing_file=routing)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('listen: "192.168.20.1:1053"', (self.data / "mihomo" / "config.yaml").read_text())

    def test_routing_dns_substitution_is_rejected_without_execution(self):
        sentinel = self.root / "executed"
        routing = self.root / "invalid-routing.env"
        routing.write_text(f'DNS_LISTEN_IPV4="$(touch {sentinel})"\n')
        self.assert_clean_failure(self.run_install(routing_file=routing))
        self.assertFalse(sentinel.exists())

    def test_download_failure_removes_stage(self):
        self.assert_clean_failure(self.run_install(DOWNLOAD_FAILURE="1"))

    def test_checksum_failure_removes_stage(self):
        self.assert_clean_failure(self.run_install(CORRUPT_DOWNLOAD="1"))

    def test_config_failure_removes_stage(self):
        self.assert_clean_failure(self.run_install(CONFIG_FAILURE="1"))

    def test_publish_failure_removes_owned_units_and_root(self):
        self.assert_clean_failure(self.run_install(RELOAD_FAILURE="1"))

    def test_existing_install_refused_without_service_changes(self):
        installed = self.data / "mihomo"
        installed.mkdir()
        marker = installed / "user-file"
        marker.write_text("keep me")
        result = self.run_install()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(marker.read_text(), "keep me")
        self.assertFalse(self.log.exists())

    def prepare_uninstall(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.log.unlink()
        script = (REPO / "uninstall.sh").read_text()
        script = script.replace("/data", str(self.data)).replace("/etc/systemd/system", str(self.units))
        script = script.replace("/run/lock/udmse-mihomo-install.lock", str(self.root / "install.lock"))
        script = script.replace("/run/mihomo-routing", str(self.root / "routing-state"))
        for original in ("/run/systemd/system", "/lib/systemd/system", "/usr/lib/systemd/system"):
            script = script.replace(original, str(self.root / original.lstrip("/")))
        (self.source / "uninstall.sh").write_text(script)
        self.mock("stat", "echo 0")
        installed = self.data / "mihomo"
        (installed / "providers" / "private.yaml").write_text("keep provider secret")
        routing = installed / "mihomo-routing.sh"
        routing.write_text('#!/bin/bash\necho "routing $*" >> "$COMMAND_LOG"\nexit "${CLEANUP_FAILURE:-0}"\n')
        routing.chmod(0o700)
        return installed

    def run_uninstall(self, **env):
        return subprocess.run([BASH, str(self.source / "uninstall.sh")], env=dict(self.env, **env), capture_output=True, text=True)

    def test_uninstall_detaches_before_stop_and_preserves_private_data(self):
        installed = self.prepare_uninstall()
        result = self.run_uninstall()
        self.assertEqual(result.returncode, 0, result.stderr)
        commands = self.log.read_text().splitlines()
        self.assertLess(commands.index("routing detach"), commands.index("stop mihomo.service"))
        self.assertLess(commands.index("stop mihomo.service"), commands.index("routing cleanup"))
        self.assertEqual((installed / "providers" / "private.yaml").read_text(), "keep provider secret")
        self.assertTrue((installed / "config.yaml").exists())
        self.assertTrue((installed / "routing.env").exists())
        self.assertEqual(list(self.units.iterdir()), [])

    def test_uninstall_refuses_unit_ownership_mismatch_before_mutation(self):
        self.prepare_uninstall()
        (self.units / "mihomo.service").write_text("somebody else's unit")
        result = self.run_uninstall()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.log.exists())
        self.assertEqual(len(list(self.units.iterdir())), 4)

    def test_uninstall_detach_failure_keeps_listener_and_files(self):
        self.prepare_uninstall()
        result = self.run_uninstall(CLEANUP_FAILURE="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("stop mihomo.service", self.log.read_text())
        self.assertEqual(len(list(self.units.iterdir())), 4)

    def test_success_prepares_only_with_private_permissions(self):
        result = self.run_install()
        self.assertEqual(result.returncode, 0, result.stderr)
        installed = self.data / "mihomo"
        self.assertEqual(installed.stat().st_mode & 0o777, 0o700)
        for name in ("config.yaml", "routing.env", ".managed-by", "admin-token"):
            self.assertEqual((installed / name).stat().st_mode & 0o777, 0o600)
        self.assertIn('listen: "192.168.1.1:1053"', (installed / "config.yaml").read_text())
        self.assertEqual(len((installed / "admin-token").read_text().strip()), 64)
        self.assertTrue((installed / "admin" / "index.html").exists())
        self.assertEqual(self.log.read_text(), "daemon-reload\n")
        self.assertFalse((self.data / "on_boot.d").exists())
        self.assertFalse((installed / ".subscription").exists())
        self.assertNotIn("private?token", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
