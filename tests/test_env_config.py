"""Regression tests for `.env` → ACL configuration plumbing.

`.env` is how a user sets the allowlist, so if dotenv loading silently fails
their ACL settings have no effect and the bridge falls back to defaults. That
is exactly what happened when `python-dotenv` was missing from the dependency
list: `cli._load_dotenv()` swallowed the ImportError, so `.env` was ignored
entirely and a user who had configured an allowlist still ran wide open.

These tests pin the contract rather than any particular file: a `.env` in the
working directory must reach `AccessControl`.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hermes_reticulum.cli import _load_dotenv  # noqa: E402
from hermes_reticulum.core.acl import AccessControl  # noqa: E402

ALLOWED = "11223344556677889900aabbccddeeff"
STRANGER = "ffeeddccbbaa00998877665544332211"


class TestDotenvReachesAcl(unittest.TestCase):
    def setUp(self):
        self._cwd = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()
        os.chdir(self._tmp.name)
        self._saved = {
            var: os.environ.pop(var, None)
            for var in ("HERMES_RETICUM_ALLOW_ALL", "HERMES_RETICUM_ALLOWED_USERS")
        }
        with open(".env", "w", encoding="utf-8") as fh:
            fh.write("HERMES_RETICUM_ALLOW_ALL=false\n")
            fh.write(f"HERMES_RETICUM_ALLOWED_USERS={ALLOWED}\n")

    def tearDown(self):
        os.chdir(self._cwd)
        for var, value in self._saved.items():
            if value is None:
                os.environ.pop(var, None)
            else:
                os.environ[var] = value
        self._tmp.cleanup()

    def test_env_file_values_reach_the_acl(self):
        _load_dotenv()
        acl = AccessControl()
        self.assertEqual(acl.mode, "allowlist")
        self.assertFalse(acl.allow_all)
        self.assertEqual(acl.allowed_users, {ALLOWED})
        self.assertTrue(acl.is_allowed(ALLOWED))
        self.assertFalse(acl.is_allowed(STRANGER))

    def test_allow_all_in_env_is_honoured_when_opted_in(self):
        with open(".env", "w", encoding="utf-8") as fh:
            fh.write("HERMES_RETICUM_ALLOW_ALL=true\n")
        _load_dotenv()
        acl = AccessControl()
        self.assertTrue(acl.allow_all)
        self.assertEqual(acl.mode, "open")
        self.assertTrue(acl.is_allowed(STRANGER))

    def test_without_env_vars_acl_is_deny_by_default(self):
        """No env settings at all must mean closed, not open.

        Deliberately does NOT call _load_dotenv(): that also consults the
        project-root .env, which is ambient developer config and would make
        this assertion environment-dependent. The property under test is the
        ACL's own default.
        """
        acl = AccessControl()
        self.assertFalse(acl.allow_all)
        self.assertEqual(acl.mode, "closed")
        self.assertFalse(acl.is_allowed(ALLOWED))


if __name__ == "__main__":
    unittest.main()
