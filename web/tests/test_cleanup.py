import io
import json
import os
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from openeyes_web import dashboard, server


class AllocationTests(unittest.TestCase):
    def setUp(self):
        server._session_ports.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.session_file = os.path.join(self.tmp.name, "sessions.json")

    def tearDown(self):
        server._session_ports.clear()
        self.tmp.cleanup()

    def test_allocator_skips_unregistered_listening_port(self):
        with patch.object(server, "_SESSION_FILE", self.session_file), patch.object(
            server, "_BASE_CDP_PORT", 9222
        ), patch.object(server, "_port_listening", side_effect=lambda port: port == 9222):
            port = server._allocate_port("new-session")

        self.assertEqual(port, 9223)
        with open(self.session_file) as f:
            self.assertEqual(json.load(f)["new-session"]["port"], 9223)

    def test_persisted_allocation_refreshes_lease_before_reconnect(self):
        with open(self.session_file, "w") as f:
            json.dump({"returning": {"port": 9333, "last_active": 1.0}}, f)
        with patch.object(server, "_SESSION_FILE", self.session_file), patch.object(
            server.time, "time", return_value=1000.0
        ):
            self.assertEqual(server._allocate_port("returning"), 9333)
        with open(self.session_file) as f:
            self.assertEqual(json.load(f)["returning"]["last_active"], 1000.0)

    def test_wildcard_probe_detects_listener_on_other_loopback_address(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            listener.bind(("127.0.0.2", 0))
            listener.listen()
            self.assertTrue(server._port_listening(listener.getsockname()[1]))
        finally:
            listener.close()


class StateFileTests(unittest.TestCase):
    def test_save_uses_private_mode_and_loads_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            with patch.object(server, "_SESSION_FILE", path):
                server._save_sessions({"session": {"port": 9333}})
                self.assertEqual(server._load_sessions(), {"session": {"port": 9333}})
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_malformed_registry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            with open(path, "w") as f:
                f.write("not json")
            with patch.object(server, "_SESSION_FILE", path):
                with self.assertRaises(json.JSONDecodeError):
                    server._load_sessions()

    def test_failed_atomic_replace_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "sessions.json")
            with patch.object(server, "_SESSION_FILE", path), patch.object(
                server.os, "replace", side_effect=OSError("disk error")
            ):
                with self.assertRaises(OSError):
                    server._save_sessions({"session": {}})
            self.assertEqual(os.listdir(tmp), [])


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_browser_heartbeat_is_persisted_before_return(self):
        session_id = "existing"
        browser = object()
        previous_started = server._cleanup_started
        server._cleanup_started = True
        server._browsers[session_id] = browser
        server._session_ports[session_id] = 9333
        try:
            with patch.object(server, "_port_alive_async", return_value=True), patch.object(
                server, "_update_session_record"
            ) as update, patch.object(
                server, "_bg", side_effect=AssertionError("heartbeat must not be backgrounded")
            ):
                result = await server._get_browser(session_id)
            self.assertIs(result, browser)
            update.assert_called_once_with(session_id, 9333)
        finally:
            server._drop_session_state(session_id)
            server._cleanup_started = previous_started


class OwnershipTests(unittest.TestCase):
    @staticmethod
    def _cmdline(*args):
        return ("\0".join(args) + "\0").encode()

    def _read_cmdline(self, data):
        def fake_open(path, mode="r", *args, **kwargs):
            if path == "/proc/123/cmdline":
                return io.BytesIO(data)
            raise FileNotFoundError(path)
        return fake_open

    def test_exact_managed_root_is_recognized(self):
        data = self._cmdline(
            "/cache/chromium/chrome",
            "--remote-debugging-port=9333",
            "--user-data-dir=/tmp/openeyes-web-chrome-9333",
        )
        with patch("builtins.open", side_effect=self._read_cmdline(data)), patch.object(
            server.os, "readlink", return_value="/cache/chromium/chrome"
        ):
            self.assertEqual(server._managed_root_port(123, 9333), 9333)

    def test_rewritten_space_separated_chromium_process_title_is_recognized(self):
        data = self._cmdline(
            "/cache/chromium/chrome --headless=new --remote-debugging-port=9333 "
            "--user-data-dir=/tmp/openeyes-web-chrome-9333 about:blank"
        )
        with patch("builtins.open", side_effect=self._read_cmdline(data)), patch.object(
            server.os, "readlink", return_value="/cache/chromium/chrome"
        ):
            self.assertEqual(server._managed_root_port(123, 9333), 9333)

    def test_child_or_wrong_profile_is_rejected(self):
        child = self._cmdline(
            "/cache/chromium/chrome", "--type=renderer",
            "--remote-debugging-port=9333",
            "--user-data-dir=/tmp/openeyes-web-chrome-9333",
        )
        wrong_profile = self._cmdline(
            "/cache/chromium/chrome", "--remote-debugging-port=9333",
            "--user-data-dir=/tmp/not-openeyes",
        )
        with patch("builtins.open", side_effect=self._read_cmdline(child)), patch.object(
            server.os, "readlink", return_value="/cache/chromium/chrome"
        ):
            self.assertIsNone(server._managed_root_port(123, 9333))
        with patch("builtins.open", side_effect=self._read_cmdline(wrong_profile)), patch.object(
            server.os, "readlink", return_value="/cache/chromium/chrome"
        ):
            self.assertIsNone(server._managed_root_port(123, 9333))

    def test_pidfd_is_opened_before_cmdline_validation(self):
        calls = []

        def open_pidfd(pid, flags):
            calls.append(("open", pid, flags))
            return 7

        def managed_port(pid, port):
            calls.append(("cmdline", pid, port))
            return None

        with patch.object(server.os, "pidfd_open", side_effect=open_pidfd), patch.object(
            server, "_managed_root_port", side_effect=managed_port
        ), patch.object(server.signal, "pidfd_send_signal") as send, patch.object(
            server.os, "close"
        ) as close:
            self.assertEqual(server._signal_managed_root(123, 9333), "mismatch")

        self.assertEqual(calls, [("open", 123, 0), ("cmdline", 123, 9333)])
        send.assert_not_called()
        close.assert_called_once_with(7)

    def test_signal_delivery_without_exit_times_out(self):
        with patch.object(server.os, "pidfd_open", return_value=7), patch.object(
            server, "_managed_root_port", return_value=9333
        ), patch.object(server.signal, "pidfd_send_signal") as send, patch.object(
            server, "_wait_pidfd_exit", return_value=False
        ) as wait, patch.object(server.os, "close"):
            status = server._signal_managed_root(123, 9333)

        self.assertEqual(status, "timeout")
        send.assert_called_once_with(7, server.signal.SIGTERM, None, 0)
        wait.assert_called_once_with(7, server._PROCESS_EXIT_TIMEOUT)

    def test_confirmed_pidfd_exit_is_success(self):
        with patch.object(server.os, "pidfd_open", return_value=7), patch.object(
            server, "_managed_root_port", return_value=9333
        ), patch.object(server.signal, "pidfd_send_signal"), patch.object(
            server, "_wait_pidfd_exit", return_value=True
        ), patch.object(server.os, "close"):
            self.assertEqual(server._signal_managed_root(123, 9333), "exited")

    def test_pidfd_error_fails_closed(self):
        with patch.object(server.os, "pidfd_open", side_effect=OSError("unsupported")), patch.object(
            server, "_managed_root_port"
        ) as managed_port, patch.object(server.signal, "pidfd_send_signal") as send:
            self.assertEqual(server._signal_managed_root(123, 9333), "failed")
        managed_port.assert_not_called()
        send.assert_not_called()

    def test_pidfd_poll_error_event_is_not_confirmed_exit(self):
        poller = Mock()
        poller.poll.return_value = [(7, server.select.POLLNVAL)]
        with patch.object(server.select, "poll", return_value=poller):
            self.assertFalse(server._wait_pidfd_exit(7, 1.0))
        poller.register.assert_called_once_with(7, server.select.POLLIN)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session_file = os.path.join(self.tmp.name, "sessions.json")
        server._session_ports.clear()

    def tearDown(self):
        server._session_ports.clear()
        self.tmp.cleanup()

    def _write_sessions(self, data):
        with open(self.session_file, "w") as f:
            json.dump(data, f)

    def test_expiration_and_untracked_age_rules(self):
        now = 1_000_000.0
        self._write_sessions({
            "expired": {"port": 9300, "chrome_pid": 10, "last_active": now - server._TTL_SECONDS - 1},
            "fresh": {"port": 9301, "chrome_pid": 11, "last_active": now - server._TTL_SECONDS},
            "reused": {"port": 9302, "chrome_pid": 12, "last_active": now - server._TTL_SECONDS - 1},
        })
        roots = [
            {"pid": 10, "port": 9300, "age": server._TTL_SECONDS + 100},
            {"pid": 12, "port": 9302, "age": server._TTL_SECONDS + 100},
            {"pid": 20, "port": 9400, "age": server._TTL_SECONDS + 1},
            {"pid": 21, "port": 9401, "age": server._TTL_SECONDS},
        ]
        signaled = []

        def fake_signal(pid, port):
            signaled.append((pid, port))
            return "mismatch" if pid == 12 else "exited"

        with patch.object(server, "_SESSION_FILE", self.session_file), patch.object(
            server, "_managed_roots", return_value=roots
        ), patch.object(server, "_signal_managed_root", side_effect=fake_signal):
            reclaimed = server._cleanup_managed_processes(now)

        self.assertEqual(reclaimed, ["expired"])
        self.assertEqual(signaled, [(10, 9300), (12, 9302), (20, 9400)])
        with open(self.session_file) as f:
            remaining = json.load(f)
        self.assertEqual(set(remaining), {"fresh", "reused"})

    def test_timeout_retains_expired_registry_record(self):
        now = 1_000_000.0
        original = {
            "timed-out": {
                "port": 9300,
                "chrome_pid": 10,
                "last_active": now - server._TTL_SECONDS - 1,
            },
        }
        self._write_sessions(original)
        roots = [{"pid": 10, "port": 9300, "age": server._TTL_SECONDS + 1}]
        with patch.object(server, "_SESSION_FILE", self.session_file), patch.object(
            server, "_managed_roots", return_value=roots
        ), patch.object(server, "_signal_managed_root", return_value="timeout"):
            reclaimed = server._cleanup_managed_processes(now)
        self.assertEqual(reclaimed, [])
        with open(self.session_file) as f:
            self.assertEqual(json.load(f), original)

    def test_expired_record_without_live_process_is_removed(self):
        now = 1_000_000.0
        self._write_sessions({
            "gone": {"port": 9300, "last_active": now - server._TTL_SECONDS - 1},
        })
        with patch.object(server, "_SESSION_FILE", self.session_file), patch.object(
            server, "_managed_roots", return_value=[]
        ), patch.object(server, "_port_listening", return_value=False):
            reclaimed = server._cleanup_managed_processes(now)
        self.assertEqual(reclaimed, ["gone"])
        with open(self.session_file) as f:
            self.assertEqual(json.load(f), {})

    def test_expired_record_with_unverified_listener_is_retained(self):
        now = 1_000_000.0
        original = {
            "unknown": {"port": 9300, "last_active": now - server._TTL_SECONDS - 1},
        }
        self._write_sessions(original)
        with patch.object(server, "_SESSION_FILE", self.session_file), patch.object(
            server, "_managed_roots", return_value=[]
        ), patch.object(server, "_port_listening", return_value=True):
            reclaimed = server._cleanup_managed_processes(now)
        self.assertEqual(reclaimed, [])
        with open(self.session_file) as f:
            self.assertEqual(json.load(f), original)

    def test_console_cleanup_sweeps_history(self):
        with patch.object(server, "_cleanup_managed_processes") as processes, patch.object(
            server, "_sweep_history"
        ) as history:
            server.cleanup()
        processes.assert_called_once_with()
        history.assert_called_once_with()


class DashboardTests(unittest.TestCase):
    def test_failed_health_check_does_not_delete_registry_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            session_file = os.path.join(tmp, "sessions.json")
            original = {"kept": {"port": 9555, "last_active": 123.0}}
            with open(session_file, "w") as f:
                json.dump(original, f)
            with patch.object(server, "_SESSION_FILE", session_file), patch.object(
                dashboard.urllib.request, "urlopen", side_effect=OSError("transient")
            ):
                self.assertEqual(dashboard._list_live_sessions(), [])
            with open(session_file) as f:
                self.assertEqual(json.load(f), original)


if __name__ == "__main__":
    unittest.main()
