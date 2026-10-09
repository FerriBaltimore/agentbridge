# Changelog

All notable changes to AgentBridge are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). "Fixture tested" means
deterministic subprocess coverage; live provider acceptance is recorded separately in
`docs/development/*-acceptance.md` and `docs/interface/provider-acceptance.md`.

Release procedure: [docs/release.md](docs/release.md). Compatibility numbers and the
AgentBridge × GrantBridge × Fullbrain v2 matrix: [docs/compatibility.md](docs/compatibility.md).

Earlier releases: [archive through 2.9.1](docs/changelog-archive.md).

## [Unreleased]

## [2.11.6] - 2026-10-09

- Queue reads expose the exact active execution targeted by queue controls, using the
  existing queue transaction. Clients no longer need a separate native-history reader
  to display or confirm queue actions; native status remains a separate observation.

## [2.11.5] - 2026-10-09

- Explicit queued steering can retain the active turn's original context and tool grants.
  It requires the exact active turn and preserves queued identity and delivery evidence.
- Default context checks, owner/version and settings guards remain strict. Lost steering
  acknowledgements retain unknown evidence without resubmitting the input.

## [2.11.4] - 2026-10-09

- Native turn reads retain the original queued request key through the exact message,
  instance and execution binding. Clients can reconcile admitted input with native items.
- Keep direct request identity and queue evidence unchanged; this read-only projection
  neither dispatches input nor creates another request record.

## [2.11.3] - 2026-10-09

- Give runnable queued input priority over disconnected native-history reads, releasing
  the existing instance lock before opening a reader. Repeated polling no longer delays
  the next message indefinitely; active execution and paused history remain readable.
- Preserve queue identity, existing recovery holds and exactly-once admission. Reads never
  dispatch input, mutate the queue or add another execution process.

## [2.11.2] - 2026-10-09

- Immediate queued replacement interrupts the exact native turn through its existing
  connection, preserving atomic owner/version checks before queue reordering.
- Retain durable dispatch evidence before native interruption. Lost acknowledgement and
  process recovery never repeat the interruption or duplicate the selected replacement.
- Advance the selected replacement only after verified native completion and cleanup;
  uncertain outcomes remain visible and paused under the existing queue contract.

## [2.11.1] - 2026-10-09

- Add host-selected on-demand native checkpoints: retain terminal and process evidence
  without copying native files or waiting for a backup before the next input.
- Keep existing required capture as the default. Credential, recovery, continuity and
  unverified-process holds remain active in both modes; request evidence is never replayed.
- Explicit capture seals only the latest stopped execution under the existing instance
  lock. Old pending attempts cannot capture newer bytes or advertise false backup coverage.
- Preserve durable identity and cursors through schema16 migration and held restore.

## [2.11.0] - 2026-10-09

- Read and reopen native Codex history with stable thread, turn and item identities.
  Forward native status, streamed text, tool activity and user-facing plans as emitted;
  exclude private reasoning and preserve safe notices for unsupported events.
- Interrupt through the active native connection; explicit scoped recovery proves the
  previous execution stopped without terminating another conversation on the account.
- Look up message admission by its original request key without dispatching it again.
  Restore missing instance metadata only after verifying the original native identity.
- Keep native observation and admission from racing; waiting for a completed run also
  waits for its owned process cleanup before another execution can start.
- Verified with deterministic subprocess tests and bundled Codex using a synthetic local
  provider. Live provider acceptance is separate; no new provider compatibility claim.

## [2.10.19] - 2026-10-06

- Bundles GrantBridge 1.0.0-rc.21: the hosted viewer streams frames over a per-attempt
  Unix socket the host relays as a WebSocket (polling stays as the fallback), taps and keys
  leave without waiting for a result, JPEG quality adapts to motion (50 while moving, one
  sharp 85 capture once still) and Chrome no longer fetches component updates or hints.
  The viewer script is now a module with `transport.js` and `words.js` beside it; both are
  served by the `asset` action.
- The managed CLIProxyAPI sidecar starts with `-local-model`, so it keeps its embedded
  model catalogues instead of fetching three remote catalogues (each with a fallback host)
  at start and every three hours. Behind a worker egress proxy those fetches were denied
  on every login start and produced six policy errors each time; the login itself was
  never affected. The control panel and its auto-update were already disabled. The bundled
  7.3.16-fullbrain.2 build still checks one remote version manifest at start,
  unconditionally; removing that last outbound attempt needs a new pinned build.
  Fixture tested on the generated command and, when the bundle archive is prepared, on the
  real sidecar behind a loopback CONNECT recorder (seven attempts before, one after).
- AV-03: `accounts.login.browser` accepts `action: "stream"`. It is relayed to GrantBridge
  exactly like `view` and returns GrantBridge's `{socket, token, expires_at}` untouched; the
  host connects to that per-attempt sandbox socket and relays frames over its own WebSocket,
  so frames no longer travel through this RPC. After the login has ended `stream` fails with
  `authentication_attempt_not_ready` like `input`; a lost GrantBridge child reports
  `authentication_outcome_unknown` like `view`. The token is never logged or stored. The
  action needs GrantBridge rc.21; an older GrantBridge answers `invalid_params`. `view`,
  `input` and `asset` are unchanged (docs/interface/mobile-login.md).

## [2.10.18] - 2026-10-06

- Bundles GrantBridge 1.0.0-rc.20: hosted frames at the device density of the given
  `viewport`, adaptive viewer poll, flicker-free painting with a tap mark, English and
  Spanish viewer copy, and the sidecar status check at most once per second per attempt.
- `accounts.login.start` (Python `start(...)`) accepts an optional `viewport`
  `{width, height, scale}` for hosted logins: the client's CSS size (320–1280 × 480–1280)
  and device pixel ratio (1–3). AgentBridge validates it (`invalid_params` otherwise, also
  on a non-hosted login) and forwards it verbatim to GrantBridge, which renders the hosted
  sign-in page at the person's real size and density instead of a fixed 390×760 phone page.
  Omitting it keeps the previous behaviour and the previous wire request.
- `accounts.login.browser` `view` relays GrantBridge's `viewport.scale`; the placeholder
  answered after a login has ended now carries `scale: 1`. `login_start` stays `2`.

## [2.10.17] - 2026-10-05

- Bundle GrantBridge rc.19: the hosted authorization viewer clears its "connection was
  interrupted" notice on the next successful screen poll instead of keeping it over a working
  sign-in until the page changes. Protocol, recipe contract and login error set are unchanged.

## [2.10.16] - 2026-10-05

- Bundle GrantBridge rc.18: the hosted authorization browser no longer reports itself as
  automated (`navigator.webdriver` stays false), so the Cloudflare check in front of the Claude
  sign-in resolves for the person using the viewer instead of looping on "Performing security
  verification". Protocol, recipe contract and login error set are unchanged.

## [2.10.15] - 2026-10-05

- Bundle GrantBridge rc.17: the hosted authorization browser renders in software only, so
  provider sign-in starts the same way on hosts with or without a GPU or graphics driver.
- A display or browser that stops before it is ready now fails at once with a safe
  `display_unavailable` or `browser_launch_failed` reason instead of hanging. The RPC
  protocol, the recipe contract and the login error set are unchanged.

## 2.10.14

- Bundle GrantBridge rc16 bounded tool-call deadlines.
- Preserve the current authorization runtime and authenticated workspace restoration.

## 2.10.13

- Bundle GrantBridge rc15 recovery for expired local connector authorization.
- Preserve the current authorization runtime and authenticated workspace restoration.

## 2.10.12

- Bundle GrantBridge rc14 process cleanup and recoverable cancellation.
- Retain authenticated native workspace restoration and existing durability components.

## 2.10.11

- Bundle GrantBridge rc13 terminal MCP cancellation and preserve durable workspace restoration.

## [2.10.10] - Local candidate

- Bundle GrantBridge rc12 MCP transport error preservation.
- Restore held workspaces from the authenticated native checkpoint origin when the session
  working directory has changed, preserving scope checks and explicit recovery release.

## [2.10.8] - Local candidate

- Bundle GrantBridge rc11 input ordering and safe provider diagnostics.
- Preserve the existing durability recovery and isolated proxy logging runtime.

## [2.10.7] - Local candidate

- Bundle GrantBridge rc.10 with verified Google mailbox binding and retryable MCP start.
- Preserve historical credential recovery and isolated proxy error logs from 2.10.6.

## [2.10.6] - Local candidate

- Keep managed writer logs beside the credential directory so request failures do not
  block credential snapshots.
- Bundle CLIProxyAPI `7.3.16-fullbrain.2`, which recognizes historical request error logs
  while retaining credentials and rejecting unknown files or links in the snapshot.

## [2.10.5] - Local candidate

- Add explicit host-only normalization of state-owned bundled login references, including
  the reviewed stateless source closure whose old cache may have been removed.
- Preserve account identity, route configuration, authentication and recovery holds while
  making historical routes eligible for credential capture after an SDK upgrade.

## [2.10.4] - Local candidate

- Bundle GrantBridge rc.9 with provider sign-in free of a persistent toolbar.
- Preserve the current authentication, execution and durability contracts.

## [2.10.3] - Local candidate

- Bundle GrantBridge rc.8 with consistent hosted authorization expiry.
- Preserve existing credential ownership, execution and recovery contracts.

## [2.10.2] - Local candidate

- Pass the private authorization-browser transport to the GrantBridge runtime.
- Prevent signal-triggered inspector activation in the trusted SDK adapter.
- Bundle GrantBridge rc.7 with transparent isolated browsing and a simpler viewer.

## [2.10.1] - Local candidate

- Add host-only normalization of verified read-only login adapter aliases before credential
  inventory/capture. Preserve account identity and proxy configuration; refuse active login,
  held credentials, writable aliases and unknown adapter bytes.

## [2.10.0] - Local candidate

- Add the owned `accounts.login.browser` channel with packaged viewer assets.
- Keep GrantBridge alive within the SDK worker and clean temporary browser state on exit.
- Preserve CLIProxyAPI token ownership, account verification and credential capture holds.
- Admit verified 2.9.1 proxy routes after upgrade; interrupt lost browser hosts without replay.
- Package the pinned GrantBridge browser runtime; no additional public server is required.
- Validate browser consent against fixtures; production provider acceptance remains pending.

[Unreleased]: https://github.com/FerriBaltimore/agentbridge/compare/v2.11.6...HEAD
[2.11.6]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.11.6
[2.11.5]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.11.5
[2.11.4]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.11.4
[2.11.3]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.11.3
[2.11.2]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.11.2
[2.11.1]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.11.1
[2.11.0]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.11.0
[2.10.19]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.10.19
[2.10.18]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.10.18
[2.10.17]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.10.17
[2.10.16]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.10.16
[2.10.15]: https://github.com/FerriBaltimore/agentbridge/releases/tag/v2.10.15
