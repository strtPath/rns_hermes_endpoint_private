"""
Hermes Integration — calls Hermes Agent to process messages and return replies.

Supports two modes:
  1. CLI mode (default): calls `hermes chat -q` as a subprocess
  2. Future: direct Python import when Hermes is importable
"""

import logging
import os
import shutil
import subprocess

logger = logging.getLogger("hermes_reticulum.hermes")

# Common locations for the hermes binary
_HERMES_CANDIDATES = [
    "hermes",  # in PATH
    "/opt/hermes/.venv/bin/hermes",
    "/opt/hermes/bin/hermes",
    os.path.expanduser("~/.hermes/bin/hermes"),
    os.path.expanduser("~/.local/bin/hermes"),
]


def find_hermes_bin() -> str | None:
    """
    Auto-detect the hermes binary path.
    Checks PATH first, then known installation locations.
    """
    # Check PATH first
    found = shutil.which("hermes")
    if found:
        return found

    # Check known locations
    for candidate in _HERMES_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


class HermesClient:
    """
    Client that sends messages to Hermes Agent and returns replies.

    Handles the subprocess call, timeout, output parsing, and error recovery.
    """

    def __init__(
        self,
        hermes_bin: str | None = None,
        timeout: int = 300,
        source_tag: str = "reticulum",
        extra_args: list[str] | None = None,
        model: str | None = None,
    ):
        """
        Args:
            hermes_bin: Path to the hermes CLI binary. Auto-detected if None.
            timeout: Max seconds to wait for a reply.
            source_tag: Source tag passed to --source for session tracking.
            extra_args: Additional args to pass to hermes chat.
            model: Model name to pin (passed as `-m <model>`). If None, uses
                `HERMES_MODEL` env var; if that's unset too, the hermes default.
        """
        if hermes_bin is None:
            hermes_bin = find_hermes_bin()
        if hermes_bin is None:
            raise RuntimeError(
                "Hermes binary not found. Install Hermes or set HERMES_BIN env var."
            )

        self.hermes_bin = hermes_bin
        self.timeout = timeout
        self.source_tag = source_tag
        self.extra_args = extra_args or []
        self.model = model or os.getenv("HERMES_MODEL", "").strip() or None
        self.session_name = os.getenv(
            "HERMES_SESSION_NAME", f"mesh-{source_tag}"
        )
        self._model_lock = __import__("threading").Lock()

        logger.info("Hermes binary: %s", self.hermes_bin)
        if self.model:
            logger.info("Hermes model pinned: %s", self.model)
        logger.info("Hermes session thread: %s", self.session_name)

    def chat(self, message: str, new_session: bool = False) -> str | None:
        """
        Send a message to Hermes and return the text reply.

        Args:
            message: The user's message text.
            new_session: If True, this call starts a fresh conversation
                thread (drops prior context). See _session_args.

        Returns:
            The agent's reply text, or None on error.
        """
        cmd = [
            self.hermes_bin,
            "chat",
            "-q",
            message,
            "--source",
            self.source_tag,
            "-Q",  # quiet — suppress banner/spinner
        ]
        # Session handling:
        #  - new_session=True  → brand-new thread, drops all prior context
        #  - new_session=False → resume the per-source named thread (continuity)
        if new_session:
            cmd += ["--pass-session-id"]
        else:
            # Continue (create-if-missing) the named thread for this source so
            # mesh conversations keep context across calls. `/new` resets it.
            cmd += ["-c", self.session_name, "--create-if-missing"]
        with self._model_lock:
            model = self.model
        if model:
            cmd += ["-m", model]
        cmd.extend(self.extra_args)

        try:
            logger.debug("Calling: %s", " ".join(cmd))

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )

            if result.returncode != 0:
                logger.error(
                    "Hermes exited with code %d: %s",
                    result.returncode,
                    result.stderr[:500] if result.stderr else "(no stderr)",
                )
                return self._error_reply(result.returncode, result.stderr)

            reply = result.stdout.strip()
            if not reply:
                logger.warning("Hermes returned empty output")
                return "_(no response)_"

            return reply

        except subprocess.TimeoutExpired:
            logger.error("Hermes timed out after %ds", self.timeout)
            return (
                f"⏱️ Processing exceeded the {self.timeout}s limit. "
                "Try a shorter question."
            )

        except FileNotFoundError:
            logger.error("Hermes binary not found at: %s", self.hermes_bin)
            return "❌ Hermes Agent not found. Check your installation."

        except Exception as e:
            logger.error("Unexpected error calling Hermes: %s", e, exc_info=True)
            return f"❌ Unexpected error: {str(e)[:200]}"

    def set_model(self, model: str | None) -> str:
        """
        Change the pinned model at runtime (used by the `/model` command).

        Returns the new active model string for confirmation.
        """
        with self._model_lock:
            self.model = (model or "").strip() or None
            active = self.model
        logger.info("Hermes model set to: %s", active)
        return active or "(hermes default)"

    def get_model(self) -> str:
        with self._model_lock:
            return self.model or "(hermes default)"

    def reset_session(self) -> None:
        """
        End the current conversation thread so the next message starts fresh.

        We can't delete the named thread in-place, so we bump the session name
        — the next `chat()` call opens a brand-new thread with no prior context.
        This is what the `/new` command triggers.
        """
        import time

        self.session_name = f"mesh-{self.source_tag}-{int(time.time())}"
        logger.info("Session reset — new thread: %s", self.session_name)

    def _error_reply(self, returncode: int, stderr: str) -> str:
        """Build a user-friendly error message from a failed Hermes call."""
        if returncode == 1:
            return "❌ Error processing message. Check the logs."
        elif returncode == 130:
            return "⏱️ Processing canceled due to timeout."
        else:
            brief = (stderr or "").split("\n")[-1][:200]
            if brief:
                return f"❌ Error (code {returncode}): {brief}"
            return f"❌ Error (code {returncode})"
