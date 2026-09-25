"""
Shared pytest fixtures for the Reticulum plugin tests.

The ``gateway`` package ships with the Hermes installation, not with this
repository. Tests run from the repo venv, so find the Hermes gateway site
package on disk and add it to ``sys.path`` before the test modules are
imported. If it is genuinely absent (CI without a Hermes install), fall
back to stubbing the few gateway modules the plugin drags in — the scoped
secret reader in a default-profile environment behaves like a plain env
read, which the stub reproduces.
"""
import os
import sys
import types

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if os.path.join(_ROOT, "src") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "src"))

def _find_gateway_root():
    """Locate a directory that directly contains the gateway package
    (the Hermes install tree ships it at the repo root, e.g.
    ~/.hermes/hermes-agent). Returns the path or None."""
    candidates = [
        os.path.expanduser(os.environ.get("HERMES_GATEWAY_ROOT", "")),
        os.path.expanduser("~/.hermes/hermes-agent"),
    ]
    for _root in candidates:
        if _root and os.path.isdir(os.path.join(_root, "gateway", "platforms")):
            return _root
    # Fallback: a gateway package installed in a venv site-packages.
    for _root in (
        os.path.expanduser(os.environ.get("HERMES_VENV_PATH", "")),
        os.path.expanduser("~/.hermes/hermes-agent/venv"),
    ):
        if not _root:
            continue
        _lib = os.path.join(_root, "lib")
        if not os.path.isdir(_lib):
            continue
        for _sp in sorted(os.listdir(_lib)):
            _candidate = os.path.join(_lib, _sp, "site-packages")
            if os.path.isdir(os.path.join(_candidate, "gateway")):
                return _candidate
    return None

_GATEWAY_SITE = _find_gateway_root()
for _root in (
    os.path.expanduser(os.environ.get("HERMES_VENV_PATH", "")),
    os.path.expanduser("~/.hermes/hermes-agent/venv"),
):
    if not _root:
        continue
    _lib = os.path.join(_root, "lib")
    if not os.path.isdir(_lib):
        continue
    for _sp in os.listdir(_lib):
        _candidate = os.path.join(_lib, _sp, "site-packages")
        if os.path.isdir(os.path.join(_candidate, "gateway")):
            _GATEWAY_SITE = _candidate
            break
    if _GATEWAY_SITE:
        break

if _GATEWAY_SITE:
    # The Hermes install tree ships the gateway package at its root, but
    # importing the top-level gateway/__init__ drags in the full agent
    # runtime. The tests only need gateway.platforms.{base,_shared,event}
    # and gateway.config, so load those files directly and register the
    # packages manually.
    import importlib.util

    def _load(name, path):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
        return mod

    _pkg = types.ModuleType("gateway")
    _pkg.__path__ = [os.path.join(_GATEWAY_SITE, "gateway")]
    sys.modules["gateway"] = _pkg
    # Pre-register the platforms package shell (without executing its
    # __init__, which would re-import .base while it is still loading)
    # so that "from gateway.platforms.helpers import ..." during
    # base.py's own execution finds the sibling module.
    _platforms_pkg = types.ModuleType("gateway.platforms")
    _platforms_pkg.__path__ = [os.path.join(_GATEWAY_SITE, "gateway", "platforms")]
    sys.modules["gateway.platforms"] = _platforms_pkg
    # The gateway modules resolve their sibling runtime packages
    # (agent/, utils.py, ...) against the install root, not against
    # the gateway package dir.
    _agent_pkg = types.ModuleType("agent")
    _agent_pkg.__path__ = [os.path.join(_GATEWAY_SITE, "agent")]
    sys.modules["agent"] = _agent_pkg
    # utils.py is a top-level module of the install tree, not a
    # subpackage: the install root itself provides it.
    if _GATEWAY_SITE not in sys.path:
        sys.path.insert(1, _GATEWAY_SITE)

    _platforms_pkg = sys.modules["gateway.platforms"]
    _platforms_pkg._shared = _load(
        "gateway.platforms._shared",
        os.path.join(_GATEWAY_SITE, "gateway", "platforms", "_shared.py"),
    )
    _platforms_pkg.base = _load(
        "gateway.platforms.base",
        os.path.join(_GATEWAY_SITE, "gateway", "platforms", "base.py"),
    )
    if os.path.exists(os.path.join(_GATEWAY_SITE, "gateway", "platforms", "event.py")):
        _platforms_pkg.event = _load(
            "gateway.platforms.event",
            os.path.join(_GATEWAY_SITE, "gateway", "platforms", "event.py"),
        )
    if os.path.exists(os.path.join(_GATEWAY_SITE, "gateway", "config.py")):
        _pkg.config = _load(
            "gateway.config",
            os.path.join(_GATEWAY_SITE, "gateway", "config.py"),
        )
    _pkg.platforms = _platforms_pkg
else:
    # No gateway package available (CI without a Hermes install): stub the
    # modules the plugin imports. The scoped reader in a default-profile
    # environment behaves like a plain env read, which the stub reproduces.
    _shared_stub = types.ModuleType("gateway.platforms._shared")
    _shared_stub.get_scoped_secret = (
        lambda name, default=None, **kw: os.environ.get(name, default)
    )
    _shared_stub.seed_extra_from_env = lambda *a, **kw: {}

    class _SendResult:
        def __init__(self, success, error=None, retryable=False, error_kind=None):
            self.success = success
            self.error = error
            self.retryable = retryable
            self.error_kind = error_kind

    class _SessionSource:
        """Attribute-accessible stand-in for the gateway's SessionSource.

        The real ``build_source`` returns a ``SessionSource`` dataclass, not a
        dict: tests read ``event.source.chat_id``. A dict-returning stub fails
        those tests, which is what makes an unfaithful stub dangerous — the
        suite goes green against the real gateway and red in CI, or vice versa.
        """

        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _BasePlatformAdapter:
        def __init__(self, config=None, platform=None, **kwargs):
            self.config = config
            self.platform = platform
            self.is_connected = False

        def _mark_connected(self):
            self.is_connected = True

        def _mark_disconnected(self):
            self.is_connected = False

        def build_source(self, **kwargs):
            return _SessionSource(**kwargs)

        async def handle_message(self, event):
            return None

    _base_stub = types.ModuleType("gateway.platforms.base")
    _base_stub.BasePlatformAdapter = _BasePlatformAdapter
    _base_stub.SendResult = _SendResult

    class _MessageEvent:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _MessageType:
        TEXT = "text"

    class _Source:
        pass

    _event_stub = types.ModuleType("gateway.platforms.event")
    _event_stub.MessageEvent = _MessageEvent
    _event_stub.MessageType = _MessageType

    _config_stub = types.ModuleType("gateway.config")

    class _Platform:
        LOCAL = "local"

        def __init__(self, name):
            self.name = name

    _config_stub.Platform = _Platform

    _platforms_stub = types.ModuleType("gateway.platforms")
    _platforms_stub.__path__ = []
    _platforms_stub._shared = _shared_stub
    _platforms_stub.base = _base_stub
    _platforms_stub.event = _event_stub
    _gateway_stub = types.ModuleType("gateway")
    _gateway_stub.__path__ = []
    _gateway_stub.platforms = _platforms_stub
    _gateway_stub.config = _config_stub
    for _name, _mod in (
        ("gateway", _gateway_stub),
        ("gateway.platforms", _platforms_stub),
        ("gateway.platforms._shared", _shared_stub),
        ("gateway.platforms.base", _base_stub),
        ("gateway.platforms.event", _event_stub),
        ("gateway.config", _config_stub),
    ):
        sys.modules.setdefault(_name, _mod)
