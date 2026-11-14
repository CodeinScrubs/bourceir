# Timing clock 2.0.0 engineering review

Date: 2026-10-24, Asia/Tehran. Scope: the standalone local clock and operator-configured one-shot desktop click. No financial order was submitted, edited, cancelled or armed by the agent. BRS trading logic and databases were not changed.

## Finding and change

The original version's happy-path checks were insufficient for a strong reliability claim. This review made concrete changes:

| Finding | Resulting behavior |
| --- | --- |
| Concurrent copies could schedule duplicates | A GUI mutex, legacy-instance check and cross-thread/process plan semaphore reject cooperating duplicates. No older process is forcibly closed by the product. |
| Root HWND alone missed movement, reused processes and hosted surfaces | Root and leaf handles, both process identities/creation times, thread IDs, geometry, DPI, display layout and active input desktop are checked. Hosted controls may belong to another process. |
| A short Escape press could evade polling | Escape is registered as a hotkey while needed; failure to reserve it prevents arming. Polling remains a fallback. |
| Cancellation could wait behind dispatch checks | Atomic phase transitions define whether cancellation or dispatch won. No successful cancellation is claimed after dispatch commitment. |
| A past time silently selected tomorrow | Clicks now require an explicit Jalali date. The next-occurrence button is a separate operator action. |
| New or interrupted point selection could retain an old point | Beginning a selection clears the old point. Interrupted selection and lifecycle changes invalidate it. Selection epochs cover races with queued lifecycle events. |
| UI stalls and preparation-time clock changes were not covered | UI heartbeat, per-step and cumulative clock checks, preparation checks, and lifecycle cancellation stop the plan. |
| Recent input could combine with a scheduled click | Held modifiers/buttons and an input-quiet interval prevent dispatch. The observation window also handles backwards timestamps and tick rollover. |
| Partial native input and callback failures were under-tested | Input dispatch is one-shot. Only a release is attempted for a known queued-down partial result. Unknown counts/times stay null; callback failures retain the worker result. |
| Post-send uncertainty could permit an immediate new plan | Ambiguous input or failed audit/timing/cleanup latches scheduling off until the operator inspects the destination and reopens the app. |
| Source hashing did not bind the bytes actually compiled | The builder compiles immutable source snapshots, verifies them and the executable, checks for concurrent source changes, and locks the verified executable through startup. |
| Hidden build/start failures could appear successful | Errors have visible/private receipts; a nonce-bound, unarmed startup receipt and launcher confirmation precede enabling scheduling. Corrupt cache recovery preserves old artifacts. |

## Verification and evidence boundaries

| Check | Result and scope |
| --- | --- |
| Original clock logic | 26 checks passed. Parsing, target precision, date rollover, limits and clock continuity. |
| Fault injection | 32 groups passed, including partial/throwing input, constructor and callback failures, cleanup, clock jumps, mutable snapshots, duplicate leases and a 128-iteration cancellation race exercise. Fakes are confined to tests and never send desktop input. |
| Coordinate properties | Every pixel roundtripped for the tested widths, including negative origins and the 65,536-pixel representability boundary. This is not a physical multi-monitor test. |
| Build sandbox | 13 checks passed: paths with spaces, cache reuse, source snapshot binding, corrupt executable recovery, preserved artifacts, malicious cache path rejection, concurrent builds, compiler failure and recovery. No application was launched in these build tests. |
| UI component workflow | Real Forms controls exercised invalid/missing input, explicit dates, hotkey conflicts, locked editing, timed selection and interrupted selection, Escape message handling, lifecycle cancellation, frozen UI and failed durable logging. A full workflow delivered one real click to its own test button. Later UI revisions were rechecked in no-input mode. |
| Native input | Three successful trials each delivered exactly one mouse-down, mouse-up and click to the local test button. Cancellation, overlay, movement, resizing, disabled target and recent injected movement each produced zero scheduled input. |
| Hosted control | A real control owned by a second test process passed root/leaf identity validation inside its host. This test sent no input and was not a broker-browser test. |
| Startup | Actual nonce/executable/unarmed startup handshake succeeded. No schedule was armed. |
| Repository checks | The full `pytest -m 'not network' -q` run exited successfully and `ruff check .` passed. These Python checks do not test the C# sender. |
| Visual inspection | Accessibility output and a rendered component image were inspected. Native computer-use screen capture returned a frame timeout; a component render is not claimed as a native screen recording. |

In the final quiet-desktop native run, software-dispatch lateness was 4.7264, 0.9753 and 0.7048 ms. The receiving test button's mouse-down lateness was 15.0575, 14.1832 and 6.8851 ms. These are three local observations, not a percentile estimate, worst-case bound or broker/exchange timing result. A previous run cancelled on recent input; that failure was preserved and the guard was not disabled to obtain a passing result.

Removing repeated process-list lookups from the deadline path retained fresh native identity and geometry checks. The process's own session ID is immutable and cached; the session's active state is still queried. Target display names are collected for operator review but omitted from deadline-path lookup.

## Unverified or irreducible limits

- A coordinate tool does not verify page contents, selected instrument, amount, login state or meaning of a button. The same window and geometry can contain a different action. There is also a residual check-to-input race.
- Windows scheduling, GC, input drivers, other automation, hardware and the destination event loop have no proven bound here. The limit controls dispatch start, not destination processing or network receipt.
- UTC accuracy, exchange-clock agreement, broker acceptance, queue rank and future fill probability remain unverified.
- Physical sleep/resume, session lock/disconnect, UAC transitions, mixed-DPI/multi-monitor hardware and alternate mouse mappings were not exhaustively exercised. Fault/UI-signal tests are not hardware evidence. Swapped mouse buttons and unrepresentable desktop dimensions are explicitly unsupported.
- Once input has been queued it cannot be withdrawn by this tool. Power loss can leave an armed receipt without a result; that is ambiguous, not proof that no click happened. Startup never replays it.
- Local hashes are integrity checks, not signed provenance or protection against a malicious user/process with equivalent access. The tool does not bypass UIPI or change security/time/network settings.

The defensible sign-off is **hardened and validated within these stated bounds**. “Bulletproof,” exact millisecond delivery, and guaranteed exchange arrival are not supported claims.
