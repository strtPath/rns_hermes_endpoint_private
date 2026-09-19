"""Tool emoji parity with the Hermes gateway (Telegram) progress bubble.

The table in ``hermes_reticulum.core.tool_emoji`` is a copy of the gateway's
registry (``agent/display.py::get_tool_emoji``), so the test that keeps it
honest is a drift check against the installed Hermes, not a snapshot: it
asserts that every tool the installed Hermes knows about resolves to the same
emoji in our table.

Run with:  ./venv/bin/python -m unittest tests.test_tool_emoji -v
"""
import json
import os
import sys
import unittest
from unittest import mock

# Ensure project src is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../src"))

from hermes_reticulum.core.tool_emoji import (  # noqa: E402
    ERROR_EMOJI,
    FALLBACK_EMOJI,
    TOOL_EMOJIS,
    reset_cache,
    tool_emoji,
    tool_label,
)


class TestToolEmojiLookup(unittest.TestCase):
    def setUp(self):
        reset_cache()

    def tearDown(self):
        reset_cache()

    def test_known_tools_match_the_gateway_table(self):
        self.assertEqual(tool_emoji("terminal"), "💻")
        self.assertEqual(tool_emoji("read_file"), "📖")
        self.assertEqual(tool_emoji("write_file"), "✍️")
        self.assertEqual(tool_emoji("patch"), "🔧")
        self.assertEqual(tool_emoji("search_files"), "🔎")
        self.assertEqual(tool_emoji("web_search"), "🔍")
        self.assertEqual(tool_emoji("execute_code"), "🐍")
        self.assertEqual(tool_emoji("delegate_task"), "🔀")
        self.assertEqual(tool_emoji("browser_exec"), "🌐")

    def test_distinct_tools_keep_distinct_emojis(self):
        # The gateway distinguishes terminal from patch from write_file; a
        # single generic wrench for everything was the bug being fixed.
        self.assertNotEqual(tool_emoji("terminal"), tool_emoji("patch"))
        self.assertNotEqual(tool_emoji("read_file"), tool_emoji("write_file"))

    def test_error_flag_wins_over_the_table(self):
        self.assertEqual(tool_emoji("terminal", is_error=True), ERROR_EMOJI)
        self.assertEqual(tool_emoji("brand_new_tool", is_error=True), ERROR_EMOJI)

    def test_unknown_tool_falls_back_without_raising(self):
        self.assertEqual(tool_emoji("brand_new_tool"), FALLBACK_EMOJI)
        self.assertEqual(tool_emoji(""), FALLBACK_EMOJI)
        self.assertEqual(tool_emoji(None), FALLBACK_EMOJI)

    def test_tool_label_is_emoji_then_name(self):
        self.assertEqual(tool_label("terminal"), "💻 terminal")
        self.assertEqual(tool_label("terminal", is_error=True), f"{ERROR_EMOJI} terminal")


class TestOperatorOverrides(unittest.TestCase):
    """An operator re-syncs after a Hermes update without a bridge release."""

    def setUp(self):
        reset_cache()

    def tearDown(self):
        reset_cache()

    def test_override_file_wins_over_the_builtin_table(self):
        with mock.patch.dict(os.environ, {"HERMES_TOOL_EMOJIS": "/nonexistent/path.json"}):
            reset_cache()
            self.assertEqual(tool_emoji("terminal"), "💻")

        with mock.patch(
            "hermes_reticulum.core.tool_emoji._load_overrides",
            return_value={"terminal": "🖥"},
        ):
            self.assertEqual(tool_emoji("terminal"), "🖥")
            self.assertEqual(tool_emoji("read_file"), "📖")  # unoverridden tools untouched

    def test_unreadable_override_file_is_ignored(self):
        import json
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
            bad_path = fh.name
        try:
            with mock.patch.dict(os.environ, {"HERMES_TOOL_EMOJIS": bad_path}):
                reset_cache()
                self.assertEqual(tool_emoji("terminal"), "💻")
        finally:
            os.unlink(bad_path)
            reset_cache()


class TestTableShape(unittest.TestCase):
    def test_table_values_are_nonempty_strings(self):
        self.assertTrue(TOOL_EMOJIS)
        for name, emoji in TOOL_EMOJIS.items():
            self.assertTrue(name, "tool name must not be empty")
            self.assertIsInstance(emoji, str)
            self.assertTrue(emoji.strip(), f"{name} has an empty emoji")

    def test_table_covers_the_tools_the_bridge_gates(self):
        # SAFE_TOOLS / RISKY_TOOLS in control_server drive what the mesh
        # operator approves; every one of those must render a real emoji
        # rather than the generic fallback. Assert PRESENCE first — a name
        # absent from the table silently passed the old version of this test
        # while rendering ⚙️ in the field.
        from hermes_reticulum.core.control_server import RISKY_TOOLS, SAFE_TOOLS

        for name in sorted(set(SAFE_TOOLS) | set(RISKY_TOOLS)):
            self.assertIn(
                name,
                TOOL_EMOJIS,
                f"{name} is gated by classify_tool() but has no emoji mapping — "
                "it would render as the generic fallback",
            )
            self.assertNotEqual(tool_emoji(name), FALLBACK_EMOJI, name)

    def test_no_table_value_is_a_bare_word(self):
        # The gateway registers xai_video_* with emoji="video"; we substitute a
        # real glyph. A bare word in the table means a bad copy from upstream.
        for name, emoji in TOOL_EMOJIS.items():
            self.assertFalse(
                all(c.isalnum() for c in emoji),
                f"{name} maps to {emoji!r}, which is a word not a glyph",
            )


class TestParityWithInstalledGateway(unittest.TestCase):
    """Drift check: our table must agree with the Hermes actually installed.

    Skips when the gateway source is not on this machine (e.g. the bridge runs
    in its own venv or on a different host than Hermes). Not a snapshot test —
    it asserts the relationship between two tables, which is what breaks when
    upstream adds or renames a tool.
    """

    GATEWAY_ROOT = os.path.expanduser("~/.hermes/hermes-agent")
    GATEWAY_TOOLS = os.path.join(GATEWAY_ROOT, "tools")

    def _gateway_tool_emojis(self) -> dict:
        """{tool_name: emoji} straight from the installed gateway registry.

        Runs the probe with the *gateway's own* interpreter when one exists:
        the bridge venv is missing gateway deps (httpx, ...), and a partial
        registry would make this test pass over half the tool surface. A run
        that cannot see a core tool fails loudly instead.
        """
        import subprocess

        if not os.path.isdir(self.GATEWAY_TOOLS):
            self.skipTest("Hermes gateway source not installed on this host")

        venv_python = os.path.join(self.GATEWAY_ROOT, "venv", "bin", "python")
        interpreter = venv_python if os.path.isfile(venv_python) else sys.executable
        code = (
            "import sys, json; sys.path.insert(0, '.');"
            "import model_tools;"
            "from tools.registry import registry;"
            "print(json.dumps({n: registry.get_emoji(n, default='')"
            " for n in registry._tools}))"
        )
        try:
            out = subprocess.run(
                [interpreter, "-c", code],
                cwd=self.GATEWAY_ROOT,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except Exception as exc:  # noqa: BLE001
            self.skipTest(f"could not read the installed gateway registry: {exc}")
        if out.returncode != 0:
            self.skipTest(f"gateway registry probe failed: {out.stderr.strip()[:200]}")

        try:
            return json.loads(out.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            self.skipTest(f"gateway registry probe returned unparseable output: {exc}")

    # Tools that must be visible in the probe for the comparison to mean
    # anything. Their absence means the probe ran against a broken environment
    # (missing gateway deps), not that the gateway lacks them.
    _SENTINELS = ("terminal", "read_file", "write_file", "web_search")

    # Tools the bridge's classification sets name but upstream never registers:
    # `todo`/`cronjob` are pre-rename aliases (todo_list/cronjob_manage are the
    # live names), `page_info` is a browser_exec helper function, and the
    # holographic-memory plugin registers fact_store/fact_feedback with no emoji
    # at all. These are deliberate local entries, not drift, so the parity check
    # does not require upstream to know them.
    _ALIASES = {"todo", "cronjob", "page_info", "fact_store", "fact_feedback"}

    def test_probe_sees_the_real_registry(self):
        """Guard against a vacuous pass over a partially-loaded registry."""
        gateway = self._gateway_tool_emojis()
        absent = [t for t in self._SENTINELS if t not in gateway]
        self.assertEqual(
            absent,
            [],
            "the gateway probe did not see core tools "
            f"{absent} — it ran in a broken environment and any parity result "
            "from it is meaningless",
        )

    def test_every_gateway_tool_matches_our_table(self):
        gateway = self._gateway_tool_emojis()
        self.assertTrue(gateway, "gateway registry returned nothing")

        def is_glyph(value: str) -> bool:
            """True for a real emoji/symbol; False for a bare word like 'video'."""
            return any(c.isalnum() is False and not c.isspace() for c in value)

        mismatched, missing = {}, []
        for name, emoji in gateway.items():
            if not emoji:
                continue  # gateway default (⚡) — our FALLBACK covers it
            if not is_glyph(emoji):
                continue  # bare-word upstream value (xai_video_*); exempt
            if name not in TOOL_EMOJIS:
                missing.append(f"{name}={emoji}")
            elif TOOL_EMOJIS[name] != emoji:
                mismatched[name] = {"gateway": emoji, "mesh": TOOL_EMOJIS[name]}

        self.assertEqual(
            mismatched, {}, f"mesh table disagrees with the gateway for: {mismatched}"
        )
        self.assertEqual(
            missing,
            [],
            "tools the gateway knows but the mesh table does not "
            f"(add them, or set HERMES_TOOL_EMOJIS overrides): {missing}",
        )

    def test_table_has_no_undeclared_extras(self):
        """Every table entry is either a registered gateway tool or a declared alias.

        The inverse of the parity check: an entry upstream never heard of is
        either a stale leftover after a tool was removed, or a name that should
        be in _ALIASES with a reason. Either way a reviewer should see it.
        """
        gateway = self._gateway_tool_emojis()
        unknown = sorted(set(TOOL_EMOJIS) - set(gateway) - self._ALIASES)
        self.assertEqual(
            unknown,
            [],
            f"table entries the installed gateway does not register: {unknown} — "
            "add to _ALIASES with a reason, or drop them",
        )
