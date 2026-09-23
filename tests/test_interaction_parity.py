"""Interaction parity tests: mesh must behave like the native gateway.

Each test exercises one interaction feature that previously only worked
on the gateway path (Telegram/Discord via the hooks system) and now works
on the mesh CLI path (hermes chat -q over a CLI child).

Run with:  ./venv/bin/python -m unittest tests.test_interaction_parity -v
"""
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hermes_reticulum.core.hermes_client import HermesClient  # noqa: E402


def make_client(**kwargs):
    kwargs.setdefault("hermes_bin", "/usr/bin/true")
    return HermesClient(**kwargs)


class TestClarifyRoundTrip(unittest.TestCase):
    """The mesh child's clarify tool must behave like the gateway's:
    push a readable question to the user, arm the gate so the next mesh
    message is treated as the answer (not a new turn), inject the answer
    into the next prompt, and clear all state when consumed or timed out."""

    def _client(self):
        client = make_client(hermes_bin="/usr/bin/true")
        client.session_name = "mesh-test"
        client._push_callback = None
        return client

    def test_format_clarify_multi_question(self):
        """_format_clarify renders a multi-question shape as numbered
        questions with indented choices, plus a 'Reply with your choice' hint."""
        client = self._client()
        args = {
            "questions": [
                {"question": "Which option?", "choices": ["A", "B"]},
                {"question": "How many?", "choices": ["1", "2", "3"]},
            ]
        }
        body = client._format_clarify(args)
        self.assertIn("1. Which option?", body)
        self.assertIn("2. How many?", body)
        self.assertIn("A", body)
        self.assertIn("B", body)
        self.assertIn("Reply with your choice", body)

    def test_format_clarify_single_question_string(self):
        """A bare string (no dict) is rendered as-is after the ❓ head line."""
        client = self._client()
        body = client._format_clarify("Just a plain question?")
        self.assertIn("Just a plain question?", body)
        # The ❓ head is added by _push_step_clarify, not _format_clarify.

    def test_format_clarify_bare_string_in_json(self):
        """A JSON string that is not a dict or list falls back to the raw
        JSON dump."""
        client = self._client()
        body = client._format_clarify('{"unexpected": "shape"}')
        self.assertIn("unexpected", body)

    def test_arm_clarify_wait_sets_pending_and_deadline(self):
        """arm_clarify_wait sets _clarify_pending=True and a future deadline."""
        client = self._client()
        self.assertFalse(client._clarify_pending)
        self.assertEqual(client._clarify_deadline, 0.0)
        client.arm_clarify_wait()
        self.assertTrue(client._clarify_pending)
        self.assertGreater(client._clarify_deadline, time.time())
        # Deadline should be roughly CLARIFY_ANSWER_TIMEOUT_S in the future.
        delta = client._clarify_deadline - time.time()
        self.assertGreater(delta, 0)
        self.assertLess(delta, client.CLARIFY_ANSWER_TIMEOUT_S + 1)

    def test_capture_clarify_answer_stashes_and_clears_pending(self):
        """capture_clarify_answer must consume the message as the answer:
        stash it in _clarify_answer, clear _clarify_pending and deadline,
        and return the answer string."""
        client = self._client()
        client.arm_clarify_wait()
        self.assertTrue(client._clarify_pending)
        answer = client.capture_clarify_answer("A")
        self.assertEqual(answer, "A")
        self.assertEqual(client._clarify_answer, "A")
        self.assertFalse(client._clarify_pending)
        self.assertEqual(client._clarify_deadline, 0.0)

    def test_capture_clarify_answer_returns_none_when_not_pending(self):
        """No open clarify → capture returns None (message is a normal turn)."""
        client = self._client()
        self.assertFalse(client._clarify_pending)
        answer = client.capture_clarify_answer("hello")
        self.assertIsNone(answer)
        self.assertIsNone(client._clarify_answer)

    def test_capture_clarify_answer_returns_none_on_empty_text(self):
        """Empty/whitespace text → None even if a clarify is pending."""
        client = self._client()
        client.arm_clarify_wait()
        answer = client.capture_clarify_answer("   ")
        self.assertIsNone(answer)
        # Pending stays set (the question is still open).
        self.assertTrue(client._clarify_pending)

    def test_capture_clarify_answer_times_out_stale_question(self):
        """A stale deadline → the answer is dropped, pending cleared,
        and None returned (the message is a normal turn)."""
        client = self._client()
        client._clarify_pending = True
        client._clarify_deadline = time.time() - 1  # already past
        answer = client.capture_clarify_answer("late answer")
        self.assertIsNone(answer)
        self.assertFalse(client._clarify_pending)
        self.assertEqual(client._clarify_deadline, 0.0)
        self.assertIsNone(client._clarify_answer)

    def test_pop_clarify_answer_returns_and_clears(self):
        """pop_clarify_answer returns the stashed answer and clears it."""
        client = self._client()
        client._clarify_answer = "option B"
        ans = client.pop_clarify_answer()
        self.assertEqual(ans, "option B")
        self.assertIsNone(client._clarify_answer)

    def test_pop_clarify_answer_returns_none_when_empty(self):
        """No stashed answer → None."""
        client = self._client()
        self.assertIsNone(client.pop_clarify_answer())

    def test_push_step_clarify_arms_gate(self):
        """_push_step_clarify must push the formatted question AND arm the
        clarify gate (_clarify_pending becomes True)."""
        client = self._client()
        pushed = []
        client._push_callback = pushed.append
        args = {"questions": [{"question": "Pick one?", "choices": ["X", "Y"]}]}
        client._push_step_clarify(args)
        # The push callback received a message.
        self.assertEqual(len(pushed), 1)
        self.assertIn("Pick one?", pushed[0])
        self.assertIn("X", pushed[0])
        self.assertIn("Y", pushed[0])
        # The gate was armed.
        self.assertTrue(client._clarify_pending)
        self.assertGreater(client._clarify_deadline, time.time())

    def test_push_step_clarify_skips_push_without_callback(self):
        """No push callback → no push, but the gate is still armed."""
        client = self._client()
        client._push_callback = None
        args = {"questions": [{"question": "Q?", "choices": ["A"]}]}
        client._push_step_clarify(args)
        # Gate armed even without a push callback.
        self.assertTrue(client._clarify_pending)

    def test_push_step_routes_clarify_to_dedicated_path(self):
        """_push_step for a clarify tool call must delegate to
        _push_step_clarify (not the generic step push)."""
        client = self._client()
        pushed = []
        client._push_callback = pushed.append
        # Non-error clarify → dedicated path.
        client._push_step("clarify", {"questions": [{"question": "Q?"}]}, "", False)
        self.assertEqual(len(pushed), 1)
        self.assertIn("Q?", pushed[0])
        self.assertTrue(client._clarify_pending)
        # The generic path would NOT have set _clarify_pending.

    def test_push_step_non_clarify_does_not_arm_gate(self):
        """A non-clarify tool call must NOT arm the clarify gate."""
        client = self._client()
        pushed = []
        client._push_callback = pushed.append
        client._push_step("terminal", {"command": "ls"}, "file1\nfile2", False)
        self.assertEqual(len(pushed), 1)
        self.assertIn("ls", pushed[0])
        self.assertFalse(client._clarify_pending)

    def test_chat_injects_clarify_answer_as_prefix(self):
        """chat() must pop the stashed clarify answer and prepend it to the
        prompt so the agent sees it as a follow-up to its own question."""
        client = self._client()
        client._resume_id = "sess-clarify"
        client._clarify_answer = "option 2"
        # Stub the subprocess call to capture the prompt.
        captured_cmds = []

        def fake_guard(cmd, anchor=None):
            captured_cmds.append(cmd)
            return "agent reply"

        client._run_with_liveness_guard = fake_guard
        # Disable step-mode detection (reads real state file) so no
        # step-through prefix is prepended to the prompt.
        client.is_step_mode = lambda: False
        client.chat("what was the answer?")
        # The command should contain the injected prefix.
        self.assertEqual(len(captured_cmds), 1)
        cmd = captured_cmds[0]
        # The prompt is the 4th element (after hermes_bin, chat, -q).
        prompt = cmd[3]
        self.assertIn("[User's answer to your question: option 2]", prompt)
        self.assertIn("what was the answer?", prompt)
        # The answer was consumed.
        self.assertIsNone(client._clarify_answer)

    def test_chat_without_clarify_answer_no_prefix(self):
        """No stashed answer → no prefix injected."""
        client = self._client()
        client._resume_id = "sess-clarify2"
        client._clarify_answer = None
        captured_cmds = []

        def fake_guard(cmd, anchor=None):
            captured_cmds.append(cmd)
            return "ok"

        client._run_with_liveness_guard = fake_guard
        # Disable step-mode detection (reads real state file) so no
        # step-through prefix is prepended to the prompt.
        client.is_step_mode = lambda: False
        client.chat("plain message")
        cmd = captured_cmds[0]
        prompt = cmd[3]
        self.assertNotIn("[User's answer", prompt)
        self.assertEqual(prompt, "plain message")


class TestSteerQueue(unittest.TestCase):
    """Steering must queue a prompt that gets prepended to the next chat()
    call, without killing the running process (the child is still alive)."""

    def test_steer_sets_pending(self):
        client = make_client()
        self.assertIsNone(client._steer_pending)
        client.steer("focus on X")
        self.assertEqual(client._steer_pending, "focus on X")

    def test_steer_strips_whitespace(self):
        client = make_client()
        client.steer("  pad me  ")
        self.assertEqual(client._steer_pending, "pad me")

    def test_steer_empty_becomes_none(self):
        client = make_client()
        client.steer("   ")
        self.assertIsNone(client._steer_pending)

    def test_pop_steer_returns_and_clears(self):
        client = make_client()
        client.steer("do this")
        self.assertEqual(client.pop_steer(), "do this")
        self.assertIsNone(client._steer_pending)

    def test_pop_steer_returns_none_when_empty(self):
        client = make_client()
        self.assertIsNone(client.pop_steer())

    def test_steer_does_not_set_stop_requested(self):
        """Steering must NOT trigger a process kill (the child is still
        running; we're just queuing a prompt for the next turn)."""
        client = make_client()
        client.steer("new direction")
        self.assertFalse(client._stop_requested)
        self.assertIsNone(client._process)

    def test_chat_injects_steer_as_prefix(self):
        """chat() must pop the steer and prepend it to the prompt."""
        client = self._client_with_steer()
        client._resume_id = "sess-steer"
        client.steer("focus on error handling")
        captured_cmds = []

        def fake_guard(cmd, anchor=None):
            captured_cmds.append(cmd)
            return "ok"

        client._run_with_liveness_guard = fake_guard
        # Disable step-mode detection (reads real state file) so no
        # step-through prefix is prepended to the prompt.
        client.is_step_mode = lambda: False
        client.chat("original message")
        cmd = captured_cmds[0]
        prompt = cmd[3]
        self.assertIn("[Operator steering] focus on error handling", prompt)
        self.assertIn("original message", prompt)
        # Steer was consumed.
        self.assertIsNone(client._steer_pending)

    def _client_with_steer(self):
        client = make_client()
        return client


class TestHoldGate(unittest.TestCase):
    """The hold gate must block chat() from returning until /go clears it."""

    def test_hold_gate_blocks_reply(self):
        """While _hold_gate is True, _apply_hold_gate must block (spin)
        until _hold_gate is cleared or the 30-min timeout."""
        client = make_client()
        client._hold_gate = True
        client._push_callback = None
        # Clear the gate from a background thread after a short delay.
        def clear_gate():
            time.sleep(0.3)
            client._hold_gate = False

        t = threading.Thread(target=clear_gate, daemon=True)
        t.start()
        # _apply_hold_gate should block ~0.3s then return the reply.
        result = client._apply_hold_gate("final reply")
        self.assertEqual(result, "final reply")
        self.assertFalse(client._hold_gate)
        t.join(timeout=5)

    def test_hold_gate_noop_when_not_set(self):
        """_hold_gate False → reply passes through immediately."""
        client = make_client()
        client._hold_gate = False
        result = client._apply_hold_gate("pass through")
        self.assertEqual(result, "pass through")

    def test_hold_gate_pushes_notification(self):
        """When the gate engages, the push callback gets a 'Held' message."""
        client = make_client()
        client._hold_gate = True
        pushed = []
        client._push_callback = pushed.append
        # Clear from a thread.
        def clear_gate():
            time.sleep(0.3)
            client._hold_gate = False

        t = threading.Thread(target=clear_gate, daemon=True)
        t.start()
        client._apply_hold_gate("reply")
        t.join(timeout=5)
        self.assertTrue(any("Held" in msg for msg in pushed))


class TestSlashCommands(unittest.TestCase):
    """Slash commands must be handled locally (never sent to the LLM)."""

    def test_hold_toggle(self):
        client = make_client()
        self.assertFalse(client._hold_gate)
        client.set_hold_gate(True)
        self.assertTrue(client._hold_gate)
        client.set_hold_gate(False)
        self.assertFalse(client._hold_gate)

    def test_steer_queued_not_sent_to_llm(self):
        """/steer queues text on the client; it is NOT sent as a prompt.
        The next chat() call injects it as a prefix."""
        client = make_client()
        client.steer("new approach")
        # The text is stashed, not sent.
        self.assertEqual(client._steer_pending, "new approach")
        # It will be consumed by the next chat() call.
        self.assertEqual(client.pop_steer(), "new approach")
        self.assertIsNone(client._steer_pending)


class TestPushStepEmoji(unittest.TestCase):
    """Pushed steps must carry the same per-tool emoji the gateway shows."""

    def test_terminal_emoji(self):
        from hermes_reticulum.core.tool_emoji import tool_emoji
        self.assertEqual(tool_emoji("terminal"), "💻")

    def test_clarify_emoji(self):
        from hermes_reticulum.core.tool_emoji import tool_emoji
        self.assertEqual(tool_emoji("clarify"), "❓")

    def test_error_emoji(self):
        from hermes_reticulum.core.tool_emoji import tool_emoji
        self.assertEqual(tool_emoji("terminal", is_error=True), "❌")
        self.assertEqual(tool_emoji("clarify", is_error=True), "❌")

    def test_unknown_tool_fallback(self):
        from hermes_reticulum.core.tool_emoji import tool_emoji
        self.assertEqual(tool_emoji("nonexistent_tool"), "⚙️")

    def test_tool_label_format(self):
        from hermes_reticulum.core.tool_emoji import tool_label
        self.assertEqual(tool_label("terminal"), "💻 terminal")
        self.assertEqual(tool_label("clarify"), "❓ clarify")
        self.assertEqual(tool_label("terminal", is_error=True), "❌ terminal")


if __name__ == "__main__":
    unittest.main()
