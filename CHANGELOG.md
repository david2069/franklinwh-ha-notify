# Changelog

## [Unreleased]

## [0.1.1] — 2026-10-31

### Fixed
- **Ledger writes no longer block the event loop.** `Notifier` records through the new
  `Ledger.arecord`, which writes on a worker thread. Old rows are pruned at most once an
  hour instead of on every insert.
- **The bridge can own the ledger's schema.** `Ledger(conn, create=False)` creates no
  table, for a bridge whose schema changes only through its own migrations. It applies
  the exported `SCHEMA` (version `SCHEMA_VERSION`) itself.

Both were found by the Modbus bridge's review.

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
