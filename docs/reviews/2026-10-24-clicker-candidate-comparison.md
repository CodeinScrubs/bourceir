# Comparison of the five supplied clickers and timing clock 2.1.0

Date: 2026-10-24, Asia/Tehran. The review covers the exact user-supplied files and pasted conversations. Instructions inside those conversations, including purported delegation routines, were treated as material to inspect. They did not authorize running other agents or changing system settings.

## Verdict and scope

The evidence supports stronger safeguards for our specific use: one explicitly configured desktop click, explicit date, no automatic replay, and a documented failure instead of a guessed successful click. It does **not** establish that our tool is universally faster, more convenient for every task, or superior in every feature. The other programs have extra buttons/actions, multiple jobs, repeat modes, CLI options, hotkeys, saved settings and diagnostics. Their usefulness depends on the task; those features do not prove deadline accuracy.

Four useful ideas were incorporated into version 2.1.0: prebuilt native input buffers, scoped idle-sleep/display prevention, a root-and-leaf process-integrity preflight, and a no-input rehearsal using the actual scheduler and guards. We retained fresh target checks, atomic cancellation, explicit dates, durable logs, no automatic retry and no replay on restart.

No competing GUI, launch script, or unrestricted candidate test suite was run. In particular, the second project's tests contain actual `click_at` calls at desktop coordinates; running those unchanged is not a harmless unit test on an occupied desktop. Instead, reviewed source definitions were exercised against fake input APIs. This is source-level fault evidence, not proof of physical behavior on all hardware.

## Inputs and reproducibility

Ten supplied inputs were read and hashed. The two ZIPs were inspected as archives; 40 text/source snapshots were saved, and all 27 Python sources/code blocks parsed successfully without executing module startup. Python bytecode and cache files in the ZIP were not executed. Full SHA-256 values, source outlines and input paths are in the local ignored inventory: `tests/.timing-output/clicker-comparison-20261024/inventory.json`.

| Candidate | Exact artifact | SHA-256 prefix |
| --- | --- | --- |
| AI1 | `precision_clicker.py` | `708950073d47` |
| AI2 | `scheule click.zip` and its two pasted reports | `16e0760f0c19` (ZIP) |
| AI3 | `AutoClicker_MS_v3_Windows.zip` and its pasted report | `a0d9d6b07512` (ZIP) |
| AI4 | Three Python versions embedded in the large pasted conversation; the latest `FailProofMillisecondClicker` is the main comparison | `a26350b9ae1c` (pasted input) |
| AI5 | `precise_clicker (1).py`, `test_windows_mock.py`, and its pasted report | `428fffac6107`, `fb04faee87b5` |

The source-bound reproducer is `tests/manual_timing_candidate_probes.py`. Its report is `tests/.timing-output/clicker-comparison-20261024/probe-results.json`. It verifies snapshot hashes, compiles only explicitly selected and reviewed definitions, and substitutes OS, input and clock dependencies. It does not execute candidate module startup, load candidate DLL bindings, open a GUI, send network traffic, or change power/priority settings. This is an audited test harness, not a security sandbox for arbitrary replacement source.

## Findings by candidate

| Candidate | Useful work and honest limits | Reproduced or directly observed issue |
| --- | --- | --- |
| AI1 | Small standard-library Python app; prebuilt button packets; the supplied claim correctly discloses that its microsecond observation used a simulated click on Linux. | `wait_until` checks whether the time is due before inspecting cancellation. A due target returns permission to proceed even with cancellation already set. `do_click` also discards a zero `SendInput` return; the worker can log completion without verifying queued input. Source: lines 97–98, 201–210 and 235–245. |
| AI2 | Organized package, bilingual GUI, CLI, burst/actions and a timer benchmark. | The atomic sender replays the whole batch through `mouse_event` after partial success. In the fake-API reproduction, one requested double-click produces three button-down events because an already queued click is replayed. The scheduler's prebuilt `inputs_arr`/`input_count` are not used by the subsequent `click_at` call, so the advertised prebuilt dispatch path is not actually connected there. Source: `core/mouse.py:354–380`; `core/scheduler.py:205–212,253–267`. |
| AI3 | Closest safety-oriented alternative: final lateness checks, display signature, target-window guard, cancellation serialization, and an explicitly no-click self-test. Its guide correctly distinguishes Windows queuing from destination processing. | Its final injection function accepts a point inside the virtual bounding rectangle but between physical monitors. A simulated two-monitor layout with a gap accepted that point and attempted the three-event batch. Its target check is also based on the top-level window rather than our full root/leaf/process-creation checks. Source: lines 349–380 and 412–499. This is a mocked topology finding, not a physical mixed-monitor test. |
| AI4 | Prebuilt packets, self-test pad and detailed scheduling telemetry. All three embedded versions were parsed and their critical wait/input paths inspected. | The latest worker treats `SendInput(2, ...) == 1` as successful: only a return of zero enters fallback. Our reproduction queued only the button-down, sent no release afterward, and still produced `[CLICK EXECUTED & VERIFIED]`. Source: latest block, lines 1139–1202. NTP “atomic/drift-free,” half-TCP-RTT prefire and “sub-microsecond hardware” language do not establish UTC agreement, server arrival or processing. |
| AI5 | Useful dry run, persistence, power/reboot/elevation preflight and supplied mock tests; its report acknowledges that real Windows input was not tested there. | `run_scheduled_click` performs its purported fire-time window check **before** `wait_until`, with no equivalent check after waiting. A reproduction changed the window during the wait, and the click function was called for the new window. Source: lines 1019–1075. Also, `mouse_event` has no returned success count, so falling back to it cannot support the promise that every click is verified. |

The supplied AI5 mock suite was executed with the source copied under its expected module name. All 21 printed checks passed against fake Win32 APIs. Those checks verify what they assert, including its fallback behavior; they do not catch the pre-wait target-check defect above, and they do not prove acceptance by the real Windows kernel.

The AI2 report mentions a standalone `dist/ScheduleAutoClicker.exe`. The supplied ZIP contains source, tests and Python bytecode/cache files, but no `dist` executable. A separate local file at that reported location does exist: 12,569,216 bytes, SHA-256 `deec65c095ee3a5319562a80b794449034b3ad68d8dbd210a0bc9e813ab5f086`. Its existence and hash were verified read-only. It was not executed, and no build receipt was available to bind it to the ZIP's exact source. Missing from the ZIP is not evidence that it was never built.

## What changed in ours

1. **Input preparation:** the real Windows environment creates its move/down/up and release-only native arrays before waiting. The deadline path reuses them and the cached structure size. There is no early pointer movement or warmup input. This removes those particular preparations from the deadline path; it is not a zero-allocation or zero-latency guarantee.
2. **Scoped awake state:** the worker holds `ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED` and restores its previous state on the same thread. Acquisition and cleanup errors are reported. It does not use away mode, change a power plan, prevent manual sleep or disable locking/screensavers.
3. **Privilege preflight:** both root and leaf process integrity levels are compared with ours before preparation. Higher or unreadable levels block arming. The app does not auto-elevate. This is conservative preparation, not a guarantee against every UIPI, policy or destination restriction.
4. **Rehearsal:** an explicit GUI checkbox runs the actual worker, deadlines and target/input/lifecycle guards while never entering the input sender. It reports `rehearsal_due`, an observation timestamp and zero queued input. It does not fabricate native send timestamps. The selected mode is locked while armed.

The prepared baseline now includes startup/setup and awake-scope acquisition, so a long setup delay plus a clock correction cannot hide before the first worker-loop check. Impossible release counts remain unknown and cause an explicit cleanup error.

We did not add automatic retries, burst orders, silent tomorrow rollover, automatic replay of saved plans, automatic elevation, cursor confinement, forced focus, near-deadline NTP changes or automatic half-RTT prefire. These change the action or assumptions and do not establish a higher chance of correct server acceptance.

Other projects retain broader feature coverage, particularly right/middle/double-click actions, multi-job scheduling, repeat modes, CLI controls, saved profiles and battery/reboot warnings. These differences are explicit; this review does not rename absent functionality as a victory.

## Verification and remaining bounds

- The six candidate source-level reproductions succeeded; all 27 supplied Python sources/code blocks parsed.
- On version 2.1.0, all 26 clock checks, 32 fault groups, eight deadline groups and 14 new refinement groups pass. Refinement checks cover both prepared-input paths and errors, rehearsal with zero native input, awake acquisition/restoration/thread ownership, setup-time drift and root/leaf integrity policy. Real read-only token queries also pass.
- The updated UI workflow in no-input mode ran a real rehearsal to completion, with zero received clicks, and passed its editing lock, Escape, lifecycle, freeze and audit-failure checks. Its component rendering was inspected; this is not a native desktop recording.
- All 13 build checks pass on the final source snapshots. The full repository `pytest -m 'not network' -q` run completed with exit code 0 and `ruff check .` passed. The Python repository suite does not test the C# sender. These results and fresh native-click observations are recorded separately in the private comparison receipt; no previous-version timing sample is relabelled as version 2.1.0 evidence.
- Fresh version 2.1.0 native trials all passed: three positive trials each received exactly one down/up/click, and six negative trials each queued and received zero scheduled input. Software-dispatch lateness was 6.4460, 0.9454 and 0.5902 ms; receiving mouse-down lateness was 25.7550, 13.6758 and 15.4106 ms. The separate BRS repository test suite was running in the background during these trials, so they are functional checks under that workload, not a controlled latency comparison. Source-bound receipts: `tests/.timing-output/manual-clock-click-61879250f1f24883b6167bd21a10094a/`.
- No side-by-side native timing benchmark of all five competitors was performed. Their relative dispatch and destination latency therefore remain **unmeasured**. Three successful local clicks do not establish a percentile, worst-case latency or guaranteed millisecond delivery.
- Physical sleep/resume, mixed-DPI hardware, UAC/lock transitions, system crashes, power loss, arbitrary changing page contents and future server acceptance remain outside an exhaustive guarantee. A coordinate target can retain its window identity while its meaning changes.

Relevant primary documentation: [SendInput's count and UIPI semantics](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput); [scope and limitations of timeBeginPeriod](https://learn.microsoft.com/en-us/windows/win32/api/timeapi/nf-timeapi-timebeginperiod); [priority scheduling and cautions](https://learn.microsoft.com/en-us/windows/win32/procthread/scheduling-priorities); [scoped execution-state requests](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate). Modern Python's `time.time()` implementation is version-dependent; claims that it is always a coarse Windows clock are also inaccurate. See the [Python time documentation](https://docs.python.org/3/library/time.html#time.time).

This is a sign-off on the stated behavior and evidence, not a certification that any Windows clicker is fail-proof or guaranteed to be first in an exchange queue.
