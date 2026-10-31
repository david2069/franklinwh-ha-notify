# Changelog

## [Unreleased]

### Docs
- `docs/DESIGN.md`: the approved design, decisions, scope against FWHAI, the tap diagram and the plan.

## [0.1.0] — 2026-10-31

### Added
- First release, built to the design approved for the FranklinWH bridges (direct-connect-bridge #374):
  - target discovery per HA instance, marking which targets can show buttons;
  - sending to many targets with a delivery count;
  - tap listener over HA's WebSocket, with reconnect and back-off;
  - single-use, expiring requests, with an event-driven wait and timeout;
  - per-event templates with variables and reset;
  - a SQLite ledger of every send and tap, kept for a number of days;
  - the `Notifier` that ties them together.
