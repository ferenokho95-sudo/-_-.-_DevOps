import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("control", ROOT / "scripts/control.py")
control = importlib.util.module_from_spec(spec)
spec.loader.exec_module(control)


class ContractTests(unittest.TestCase):
    def test_compose_layers_and_storage(self):
        app = json.loads((ROOT / "compose.app.json").read_text())
        mon = json.loads((ROOT / "compose.monitoring.json").read_text())
        self.assertEqual(set(app["services"]), {"db", "kuma"})
        self.assertEqual(len(mon["services"]), 6)
        self.assertNotEqual(app["name"], mon["name"])
        self.assertIn("db-data:/var/lib/mysql", app["services"]["db"]["volumes"])
        for stack in (app, mon):
            self.assertTrue(stack["networks"]["metrics"]["external"])
            self.assertNotIn("configs", stack)
            for svc in stack["services"].values():
                self.assertEqual(svc["restart"], "unless-stopped")
                self.assertNotIn("latest", svc["image"])
                for port in svc.get("ports", []):
                    self.assertTrue(port.startswith("127.0.0.1:"))

    def test_kuma_uses_mariadb_not_sqlite(self):
        templates = json.loads((ROOT / "config-content.json").read_text())
        db = json.loads(templates["db-config"]["content"])
        self.assertEqual(db["type"], "mariadb")
        self.assertEqual(db["hostname"], "db")
        self.assertEqual(db["dbName"], "kuma")
        app = json.loads((ROOT / "compose.app.json").read_text())
        self.assertIn("db-config.json", app["services"]["kuma"]["command"][-1])

    def test_dashboard_health_matches_chaos_oracle(self):
        board = json.loads((ROOT / "monitoring/dashboard.json").read_text())
        self.assertEqual(board["panels"][0]["targets"][0]["expr"], control.QUERIES["health"])
        self.assertIn("or vector(0)", control.QUERIES["health"])
        for panel in board["panels"]:
            self.assertEqual(panel["datasource"]["uid"], "prometheus")

    def test_api_runs_on_host_docker_network_with_secret_in_stdin(self):
        with patch.dict(os.environ, {"GRAFANA_PASSWORD": "private"}), patch.object(control, "docker", return_value='{}') as call:
            control.api("/api/health")
            args, kwargs = call.call_args
            self.assertIn("uptime-metrics", args)
            self.assertNotIn("private", " ".join(args))
            self.assertIn("Authorization", json.loads(kwargs["data"])["headers"])

    def test_no_health_series_cannot_pass(self):
        with patch.object(control, "query", return_value=[]):
            with self.assertRaises(ValueError):
                control.value("up")

    def test_prometheus_error_cannot_pass(self):
        with patch.object(control, "api", return_value={"status": "error"}):
            with self.assertRaises(RuntimeError):
                control.query("up")

    def test_stage_copies_to_daemon_volume_over_stdin(self):
        with patch.dict(os.environ, {"DB_PASSWORD": "a"*48, "EXPORTER_PASSWORD": "b"*48}), patch.object(control, "docker") as call:
            control.stage("app")
            runs = [c for c in call.call_args_list if c.args[0] == "run"]
            self.assertEqual(len(runs), 2)
            for run in runs:
                self.assertIn("none", run.args)
                self.assertTrue(any(a.startswith("uptime-config-") for a in run.args))
                self.assertNotIn("${", json.loads(run.kwargs["data"])["content"])

    def test_invalid_credentials_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "credentials"
            path.write_text("DB_PASSWORD=unsafe\n")
            with patch.dict(os.environ, {"CREDENTIALS_FILE": str(path)}):
                with self.assertRaises(ValueError):
                    control.credentials()

    @patch.object(control, "api", return_value={"dashboard": {"panels": [{"targets": [{"expr": control.QUERIES["health"]}]}]}})
    def test_chaos_failure_always_recovers_target(self, _api):
        state = [{"HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}, "RestartCount": 0}]
        def fake_docker(*args, **kwargs):
            return json.dumps(state) if args[0] == "inspect" else ""
        with patch.object(control, "validate"), patch.object(control, "container_id", return_value="cid"), patch.object(control.time, "sleep"), patch.object(control, "value", return_value=1), patch.object(control, "wait", side_effect=TimeoutError), patch.object(control, "docker", side_effect=fake_docker) as docker:
            with self.assertRaises(TimeoutError):
                control.chaos("kuma", "crash")
            self.assertIn(unittest.mock.call("start", "cid", check=False), docker.call_args_list)
            self.assertIn(unittest.mock.call("unpause", "cid", check=False), docker.call_args_list)

    def test_docker_errors_are_useful_and_redact_credentials(self):
        import subprocess
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="denied password=super-secret")
        with patch.object(control.subprocess, "run", return_value=result), patch.object(control, "SECRETS", ["super-secret"]):
            with self.assertRaises(RuntimeError) as raised:
                control.docker("compose", "up")
            self.assertIn("denied", str(raised.exception))
            self.assertIn("[REDACTED]", str(raised.exception))
            self.assertNotIn("super-secret", str(raised.exception))

    @patch.object(control, "api", return_value={"dashboard": {"panels": [{"targets": [{"expr": control.QUERIES["health"]}]}]}})
    def test_crash_signals_verified_host_pid_instead_of_docker_stop_api(self, _api):
        import contextlib
        import io
        states = iter([
            {"HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}, "RestartCount": 0},
            {"State": {"Pid": 12345, "Running": True}},
            {"RestartCount": 1},
        ])
        def fake_docker(*args, **kwargs):
            return json.dumps([next(states)]) if args[0] == "inspect" else ""
        with patch.object(control, "validate"), patch.object(control, "container_id", return_value="target-cid"), patch.object(control.time, "sleep"), patch.object(control, "wait"), patch.object(control, "value", side_effect=[1, 1, 0, 0, 1, 1]), patch.object(control, "docker", side_effect=fake_docker) as docker, contextlib.redirect_stdout(io.StringIO()):
            control.chaos("kuma", "crash")
            self.assertFalse(any(call.args[0] in ("stop", "kill", "restart") for call in docker.call_args_list))
            killer = next(call for call in docker.call_args_list if call.args[0] == "run")
            self.assertIn("--pid=host", killer.args)
            self.assertEqual(killer.args[-2:], ("12345", "target-cid"))
            # Check that the helper refuses an invalid PID before sending a signal.
            import subprocess
            helper = subprocess.run(
                [os.sys.executable, "-c", killer.args[-3], "0", "target-cid"],
                capture_output=True, text=True,
            )
            self.assertNotEqual(helper.returncode, 0)


if __name__ == "__main__":
    unittest.main()
