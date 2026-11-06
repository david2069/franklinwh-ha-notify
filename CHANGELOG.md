# Changelog

## [Unreleased]

## [0.1.4] — 2026-11-07

Fixes from a peer review.

### Fixed
- **Two `wait()`s on one request both get the answer.** A second wait replaced the
  first's future, and the first's clean-up removed the second's. Each request now keeps
  a list of waiters; an answer resolves them all and each removes only its own.
- **An ask no phone received is discarded.** `Notifier.ask` returned None but left the
  request open (and, with persistent requests, in `notify_requests`), so `pending()`
  listed it. New `Requests.discard` / `adiscard`.
- **A tap handler runs in its own task.** The listener awaited it inline, so a handler
  that called `ask_and_wait` couldn't receive its own answer on that instance until the
  timeout. Running tasks are held until done; a failing one is logged and the socket stays.
  Taps still resolve in arrival order, but the ledger rows of taps handled at the same
  time may be written in either order.
- **A malformed tap event no longer drops the socket.** A non-string `action` (or an
  event without `data`) raised outside the handler's guard; it is now ignored.
- **The listener checks the `subscribe_events` result.** A refused subscription is now
  an error (`SubscribeError`, in `last_error`) with a retry, not a silent `connected`.
- **Back-off starts again after a session that was connected** and then failed, instead
  of carrying on from the last failure's step.
- **`close()` takes the notifier's lock**, so an `add_instance` in progress can't start a
  listener that outlives it.
- **A template typo such as `{x.y}`, `{x[k]}` or `{0}` sends the text as written**, as
  documented, instead of raising.
- **A send with no targets is recorded as SKIPPED ("no targets")**, as the ledger docs
  say, not ERROR (#4).

### Docs
- README: the ledger's connection is opened with `check_same_thread=False` (the ledger
  writes on a worker thread; the `Ledger` docstring now says so), the example imports
  `os`, and `ask()` returning None is handled before using `req.id`.

## [0.1.3] — 2026-11-04

### Added
- **Add, change or remove an HA instance on a running `Notifier` (#5).**
  `await n.add_instance(instance)` adds an instance, or replaces the one with the same id;
  while the notifier is listening, a new instance gets its tap listener at once, and a
  changed URL or token restarts that listener so it signs in again (a name change keeps
  the connection). The token is compared as `bearer()` gives it now against what the
  listener last signed in with, so a token callable that reads the bridge's store works;
  `Listener.token_used` holds that last token. `await n.remove_instance(instance_id)` removes it and closes its
  listener. `await n.set_instances(dict)` syncs to a whole set. `listener_status()` shows
  the change straight away. Before this, an instance added in a bridge's UI received
  sends but no taps until the bridge restarted.

## [0.1.2] — 2026-11-04

### Added
- **Requests that survive a restart (#6).** `Notifier(..., requests=Requests.persistent(conn))`
  keeps open requests in the `notify_requests` table as well as in memory. Opening,
  answering and purging write through (off the event loop from `Notifier`, through the new
  `Requests.aopen` / `aresolve`), and construction loads the rows still held. A tap inside
  the TTL after a restart resolves to the original request and runs its `on()` handler; a
  tap after expiry is still recorded as expired. In memory stays the default.
- `SCHEMA_VERSION` is 2: `SCHEMA` adds `notify_requests`. It only adds, so a bridge on
  version 1 migrates by applying `SCHEMA` again. `Requests.persistent(conn, create=False)`
  leaves the table to that migration.
- `Notifier.pending(event=None)`: requests still open, and answered ones nobody has collected.
- `Notifier.wait(request, timeout_s)`: the waiting half of `ask_and_wait`, with the same
  "no reply" ledger row. It waits no longer than the request has left, times out at once
  for a request that expired unanswered, and returns an answer already given.
  `ask_and_wait` is now `ask` + `wait`.

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
