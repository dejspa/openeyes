"""Regression: agents sharing a cwd or explicit ID must not share Chrome."""
import os
import subprocess
import sys
import unittest
import uuid
from unittest.mock import patch

from openeyes_web import server


class SessionIsolationTests(unittest.TestCase):
    def child_session(self):
        env = dict(os.environ)
        env.pop("OPENEYES_WEB_SESSION", None)
        result = subprocess.check_output(
            [sys.executable, "-c", "from openeyes_web import server; "
             "print(server._session_id(None))"], env=env, text=True,
        )
        return result.strip()

    def test_two_processes_in_same_cwd_have_distinct_sessions(self):
        self.assertNotEqual(self.child_session(), self.child_session())

    def test_identity_stays_stable_between_calls(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(server._session_id(None), server._session_id(None))

    def test_cached_legacy_pi_override_is_not_a_shared_identity(self):
        with patch.dict(os.environ, {"OPENEYES_WEB_SESSION": "pi-openeyes"}):
            self.assertNotEqual(server._session_id(None), "pi-openeyes")
            self.assertEqual(server._session_id(None), server._session_id(None))

    def test_explicit_identity_rejects_concurrent_process_and_recovers(self):
        sid = "isolation-test-" + uuid.uuid4().hex
        server._claim_session(sid)
        script = (
            "from openeyes_web import server; import sys; "
            "server._claim_session(sys.argv[1])"
        )
        try:
            result = subprocess.run([sys.executable, "-c", script, sid],
                                    capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("already owned", result.stderr)
        finally:
            os.close(server._session_owner_fds.pop(sid))
        result = subprocess.run([sys.executable, "-c", script, sid],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_different_id_can_be_owned_concurrently(self):
        sid = "isolation-test-" + uuid.uuid4().hex
        server._claim_session(sid)
        try:
            result = subprocess.run(
                [sys.executable, "-c", "from openeyes_web import server; "
                 "import sys; server._claim_session(sys.argv[1])", sid + "-other"],
                capture_output=True, text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            os.close(server._session_owner_fds.pop(sid))
