# franklinwh-ha-notify

Shared by the FranklinWH Direct Connect bridge and the Modbus bridge. Design: direct-connect-bridge #374 (approved 2026-10-31).

- No FranklinWH device code here. Bridges supply handlers, gates and storage.
- Taps arrive over HA's WebSocket only. Don't add a webhook or an unauthenticated route.
- Platform agnostic: no per-platform button caps or special cases.
- Branch and PR for every change; never commit to main. Every change has a test that fails without it.
- Public repo: no serials, IPs, emails, hostnames or tokens in code, tests, docs or commits.
