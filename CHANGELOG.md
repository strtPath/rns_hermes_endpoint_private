# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.2.0] — 2026-09-19

### Added
- **Tool gate reliability**: split the *deny* and *timeout* block messages, so an
  unanswered approval prompt tells the model it may continue on safe tools
  instead of wedging every subsequent tool call.
- **Startup gate-timeout validation**: the bridge fails loudly at boot if
  `MESH_GATE_TIMEOUT` and `HERMES_MESH_APPROVAL_TIMEOUT` are mis-ordered or
  non-positive (rejects `0`, negatives, `NaN`, infinity).
- **Jev pre-gate triage**: optional automatic pre-approval of routine/gated
  tools (`MESH_GATE_TRIAGE=allow_benign`), with a documented confidence floor.
- **Tool emoji parity**: mesh tool output uses the same emojis as the gateway's
  tool annotations.
- **Periodic re-announce**: configurable announce cadence (`RETICULUM_ANNOUNCE_INTERVAL`)
  and an `/announce` command, hardened against invalid values.
- **Service installer**: `install.sh` can auto-render the systemd user service
  for the current user.
- **Liveness marker refresh**: keeps long `-q` turns alive past the idle timeout
  by refreshing the marker on real tool activity.
- The control server's `/status` now reports its effective `approval_timeout_s`,
  and the gateway plugin validates its timeout against it at load.

### Changed
- Raised runtime floors to the current releases: `rns>=1.5.4,<2.0` and
  `lxmf>=1.1.1,<2.0`.
- README deployment docs now spell out the required three-layer gate-timeout
  ordering (`approval <= gate < hook_callback < 600`) and warn that the shipped
  defaults are not safe to run as-is.

### Fixed
- Gate wedge when an approval prompt was left unanswered while AFK — the hook
  wrapper could fire before the control-server deny clock, failing closed every
  tool for the rest of the turn.
- Rejected invalid timeout values that previously parsed as floats but were
  semantically meaningless (e.g. negative or non-finite gate timeouts).

[0.2.0]: https://github.com/strtPath/rns_hermes_endpoint/releases/tag/v0.2.0
