# TimingClock 2.1.0 external reviews: adjudication and 2.1.1 fixes

Reviewed on 2026-10-24 on Windows PowerShell 5.1 / .NET Framework. This document concerns the separate, generic desktop timing tool. No broker/account tab, order, credentials or account state was used for testing.

## Result and scope

The reviews contained useful, reproducible findings, incorrect Windows API facts, speculative performance explanations and proposed changes that weaken the final checks. Six regressions were first reproduced against the unchanged 2.1.0 production sources: the original run failed all six. They pass after the fixes. The follow-up suite now has 16 groups, including cancellation barriers, concurrent cancellers, shutdown timeout, timezone folds/gaps, native structure layout and input arriving during validation. Ten additional filesystem checks exercise actual disposable NTFS junctions.

Version 2.1.1 fixes the reproduced faults and the cancellation publication/commit boundary. It keeps the existing no-retry policy and final target validation. It does not establish a 1 ms delivery guarantee, superiority in every feature, or an exchange queue-position guarantee.

Review inputs were treated as evidence and proposals, not executable instructions. Their SHA-256 values, in user-provided order:

| Input | SHA-256 |
| --- | --- |
| Muse/Spark review | `0c4cf7031a635c495de3703ee5b59b12df964e36bdd027a595a2dd3bf3a96ca9` |
| Architecture/performance review | `5cb0e35e339357a0f1770e2540ececa1385c37637aa29d6cb8504255b8163fb9` |
| Independent conservative review | `1c0944f541370df25b7eb4079401093ae5524d36105ad4d4502e37bc53773819` |
| Companion diff | `61e0bb7e9b37162d0085c32ab6283fc5d48696508c199c6611c7d2cf9fb0bca0` |

The original reviewed archive SHA-256 is `aad340cbae714ad5789522e5f05f91d5b50327e59981b052c95830165e080ca1`. It has 61 manifested files plus the manifest. Its C# source hashes were `925868d9...3baf4`, `d23bcdf7...c083`, `6924286c...680b`. Historical reports are not relabeled as fresh 2.1.1 evidence.

## Muse/Spark findings

| Finding | Decision, correction and evidence |
| --- | --- |
| P0-1: health callback can change the target after validation | Confirmed with a deterministic reproduction. All application health callbacks now precede the final full target capture; native held-input, fresh-input, Escape and continuity checks still follow it. Regression rejects the changed target without input. The unavoidable native check-to-send interval remains. Merely adding another check followed by another arbitrary callback would not fix the ordering. |
| P1-1: any thrown send should trigger release | The hypothetical test sink can throw with an unknown queued count; the shipped prepared native sender returns a count and does not throw between separate down/up calls. An unknown count does not establish that this tool owns a held button. We retain ambiguity, latch further scheduling off, and do not inject a guessed extra release. Confirmed count=2 still triggers exactly one release-only attempt. This suggested catch patch was not adopted. |
| P1-2: Dispose can return while a worker remains active | Confirmed. Start/Dispose now coordinate through a short startup lock. A bounded join can still time out, but `IsWorkerActive`, `DisposeTimedOut` and `ShutdownError` expose that state; a subsequent Dispose can join again. Cancellation remains effective before commitment, resources stay owned until the worker finishes, and event disposal is guarded once. A blocked-guard regression verifies timeout, zero input and eventual one-time cleanup. This is not forced termination of an unbounded callback. |
| P1-3: handle reuse and process display name | Partly valid as a limitation. Added root/leaf class checks and final process-creation/class re-reads. `ProcessName` is explicitly display-only; final validation intentionally does not open another Process object for that label. We do not collect window titles: titles can contain private data, change normally and still cannot prove content identity. Same-class, same-process HWND reuse and changed browser DOM/meaning remain possible. |
| P1-4: failed awake-state restore cannot retry | Confirmed. A failed restore no longer marks the lease restored. The owning worker makes at most one additional attempt, retaining the initial failure even if recovery succeeds. A foreign thread cannot restore live state. The claim that this necessarily holds the machine awake for the whole process lifetime overstates a thread-scoped request; the worker normally exits shortly afterward. |
| P1-5: missing INPUT layout assertion | Valid hardening. Added startup checks and testable sizes: INPUT 40/28 bytes and MOUSEINPUT 32/24 bytes for 64/32-bit processes. Incorrect `cbSize` makes SendInput fail according to Microsoft; the review's claim that Windows necessarily reads a misaligned array is unsupported. Fresh native click evidence below is x64 evidence. |
| P1-6: flag 2 is manual-reset, not high resolution | Incorrect: Microsoft defines manual-reset as 0x1 and high-resolution as 0x2. Existing code already requested high resolution. Added a named constant and regression. Changing to 3 only also requests manual reset; it is not the claimed fix. No blanket timeBeginPeriod call was added. Timer-construction fallback is reported by `HighResolutionTimer=false`; a subsequent failure to arm an acquired timer aborts. Detailed construction-error diagnostics remain a possible improvement, not a timing guarantee. |
| P1-7: recorded verifier says 52, final manifest says 61 | Valid evidence-labeling gap. The old exporter ran safe checks before adding nine evidence/metadata files, then verified the final archive separately without including that final output. The numbers refer to different snapshots. The exact original 61-file archive was re-extracted and checked again in this audit. New exports keep final sealed-archive validation outside the archive and bind it to the ZIP hash, avoiding self-reference. |
| P2-1: count=2 then release=0 lacks explicit cleanup warning | Confirmed and fixed: `release_not_queued`. Counts above one remain unknown/invalid; exceptions remain unknown. No click retry. |
| P2-2: Packets outside the send try | Not an ambiguous-send bug: construction occurs before input, uses the plan's immutable snapshot, and failure is rejected without claiming any native call. Moving it into the catch that says input is ambiguous would misclassify a known pre-input failure. Native batches remain prebuilt. |
| P2-3: Pause accepts NaN/overflow | Valid internal hardening; existing callers were bounded. Rejects nonfinite, negative and oversized waits with checked conversion. Added tests. Cached the WaitHandle array to avoid per-pause array allocation. |
| P2-4: token size probe does not check error 122 | Valid hardening, not a demonstrated privilege bypass. Require the expected insufficient-buffer response and preserve SetLastError. Real own-token and nonexistent-process tests pass. |
| P2-5: double-click setting change cancels | Deliberate conservative policy. The existing explicit reason is `mouse_settings_changed`, so the review's “indistinguishable” wording is inaccurate. Kept the change detector. |
| P2-6: Cancel after completion | Clarified contract: true means no subsequent native input from this schedule, including completed no-input/rehearsal outcomes; false means native commitment occurred or input may already have been sent. It does not mean a worker is still running. |
| P2-7: Start checks precede CAS | No unsafe dispatch demonstrated. Concurrent cancellation/start rejection remains terminal. Added startup/disposal coordination; do not hide invalid start attempts. |
| P2-8: disposal ordering and double-disposal | Successful lease disposal is idempotent, including a later foreign-thread no-op. A live lease still requires its owner thread. Cancel signal disposal is now explicitly guarded once. |
| P2-9: NativeError=0 on partial sends | Count remains authoritative. NativeError is raw diagnostic data; zero is not proof of success or the absence of UIPI. No invented non-Windows error code replaces an unknown native explanation. |
| P2-10: lifecycle signals cancel even for small changes | Intentional policy retained. Weakening it changes the safety contract and requires a separate design decision. Selecting a target again after a signal is explicit. |
| P2-11: real fixture lacks lateness upper assertion | Valid test gap. The success fixture now requires sampled send lateness and receiver lateness within its 100 ms acceptance bound. This is an observation criterion, not a product guarantee for third-party receivers. |
| P2-12: evidence/test weaknesses | Strengthened exact coordinate assertions, complete Persian/Arabic target strings, real compiled Persian text, dynamic clock-test counting and executable-path selection. Added class, restoration, cancellation, timezone, wait and junction regressions. Existing fake clock/guard tests are logic tests; native delivery, two-process capture and no-input Forms tests are separately identified. OS-delivered lock/sleep/hotkey events, other display layouts and actual UIPI rejection were not physically exercised. Repeated deterministic JSON is expected and not proof that a rerun did not happen; fresh execution logs provide the run context. |
| P2-13: missing repeats, holds, persistence, Gregorian entry | Scope/features, not correctness failures for the chosen one-shot workflow. Other tools can offer those features; “better at everything” is not supportable. No replay after restart was added. |

## Architecture/performance review

| Claim | Assessment |
| --- | --- |
| Final target capture costs time after the target | Correct tradeoff; the measured path includes validation cost. We keep a fresh target check. The “nearly 100% failure with 1 ms on most systems” claim has no supporting distribution. Two of three original software samples were below 1 ms; none prove a bound. |
| Long waits can exceed the cumulative 100 ms drift budget | The conditional arithmetic is correct (50 ppm over 2700 s is 135 ms). A universal “30–100 ppm on standard PCs” claim is unsupported; QPC is not necessarily a raw fixed TSC crystal. The current policy cancels when total wall/monotonic divergence exceeds 100 ms, including gradual adjustment. A 200 ppm growing allowance changes that policy. Kept it explicit; do not arm for hours and expect corrections to be ignored. |
| Single move/down/up batch caused the observed 13–25 ms receiver delay | Not established. No controlled comparison, event trace or preposition experiment was supplied. Target activation can consume clicks in some applications, but there is no universal React/Angular hover prerequisite. No forced focus, early pointer move or cursor clamp was added. |
| 250 ms UI heartbeat can cancel a healthy worker | Yes: responsiveness is part of the configured safety policy. A 2 s allowance would permit a much longer unresponsive cancellation interface. Kept the policy and tested it. |
| Recent input cancels even for tiny movement | Correct by design. Clipping the mouse is not equivalent protection against mixed user input or double-click interpretation; it also interferes with the operator. Kept the quiet interval. |
| Disabled native controls can change the leaf HWND | Possible; strict identity then rejects. Browser DOM buttons are not generally separate native HWNDs, so a native leaf example does not establish a market-open browser bug. A coordinate guard cannot certify DOM meaning. |
| No priority tuning and final-loop overhead | Correct observations do not prove the cause of a particular delay. Cached the wait array. Did not use process realtime priority, undocumented timer tuning or delete final cancellation/input checks based on speculation. |
| Global Escape reservation can conflict/consume Escape | Correct, intentional emergency cancellation while selecting/armed. Conflict prevents arming and the reservation is released. No global input hook or key stealing outside that interval was introduced. |
| Phase is published before cancellation reason | Confirmed source race. Replaced separate phase/reason publication with one immutable state object and CAS. Merely writing the reason first lets a losing canceller overwrite the winner's reason, so that suggested shortcut is also incorrect. Removed the 25 ms publication wait. |
| BOM-less C# corrupts Persian on ANSI 1252 | Incorrect here. C# attempts UTF-8 first. Windows PowerShell 5.1 on this machine reports ANSI codepage 1252, and a production private UI-string method plus full digit/date parsing pass through the actual compiler. `/utf8output` concerns compiler diagnostics, not source decoding. Rewriting staged bytes to add a BOM would also break the snapshot hash contract unless the entire build identity changed. |
| Suggested T-35/T-25/T-5 replacement guarantees <1 ms/immediate delivery | Not demonstrated and not a drop-in implementation (`PrePositionCursor` is absent). It introduces a stale validation interval, early real input and weaker final checks, and removes safeguards used by rehearsal. Rejected. NTP or a prefire offset cannot prove exchange/server arrival. |

## Conservative review and supplied diff

F-01 (commit ordering), F-02 (release-zero), F-03 (deadline evidence), F-04 (reparse containment), F-05 (timezone gaps/folds), F-06 (retryable restoration), F-07 (cleanup aggregation) were valid findings or useful hardening. Implemented and tested them. The companion diff correctly proposed signed timing measurements, a separate early outcome and explicit release-zero failure; those ideas were adapted into the larger cancellation-safe change instead of applying the diff blindly.

Commit now occurs in the dispatcher after its final deadline sample, immediately before the prepared send. An accepted cancellation before that gate prevents input; a cancellation after commitment cannot promise to retract input. Rehearsal never claims native dispatch. Cleanup failures append with stage names instead of overwriting one another. `DispatchCheckAt`, signed lateness and final observation time are journaled, including rejected deadlines.

Generated paths now reject existing reparse points throughout workspace-to-output components in PowerShell and the C# journal/receipt paths. Actual root/intermediate junction tests confirm no writes reach the redirected target. This is accidental-redirect hardening; it is not a handle-based sandbox against a malicious same-user process changing paths between checks and opens. Normal launch remains an editable-source developer build path, not a signed immutable release launcher; package verification is a separate explicit check.

## Fresh evidence and limits

- Clock checks: 29; launcher checks: 4; fault groups: 32; deadline groups: 8; refinement groups: 14; external-review groups: 16; build checks: 13; junction/path checks: 10. Reports and source-binding manifests accompany the review export.
- No-input Forms workflow: 35 assertions, zero received clicks. Lifecycle/hotkey handler calls in this fixture are simulated notifications. Separate real two-process hosted-control capture passed without input.
- Fresh native fixture: three success trials, each exactly one down, up and click. Six negative scenarios each received zero clicks. Guard reasons intentionally group resize under `target_moved`, and unavailable/disabled identity under `target_changed` or `target_unavailable`.
- Software sample lateness: **4.3321, 0.7503, 0.5908 ms**. Receiver MouseDown lateness: **25.3840, 13.8615, 16.1212 ms**. High-resolution timer was available in all these trials. These are x64, one-machine, owned-button observations under ordinary desktop activity; no comparative performance claim or percentile follows.
- A software time sample precedes the actual native call and can itself be followed by descheduling. Queue acceptance, target processing, broker receipt, exchange acceptance and queue rank are different evidence. This tool proves none of the latter financial events.
- No real system clock changes, session lock, suspend, monitor hotplug, security-setting changes or elevated-account actions were performed. Partial native counts and restore failures remain fault-injection tests. Same native window can show changed semantic contents. An application can ignore or consume a click.

The original ZIP's stale 52-file safe-check log was not a valid receipt for all 61 final files. Its hashes were still correct. The recheck log and new sealed archive's external validation sidecar distinguish those facts. An unsigned manifest detects accidental corruption, not malicious replacement of both payload and manifest.

## Primary references checked

- [CreateWaitableTimerExW: flags 0x1 and 0x2](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createwaitabletimerexw).
- [SendInput: returned count, cbSize, UIPI and serial insertion](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput).
- [SetThreadExecutionState: thread requirements and return value](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate).
- [C# CodePage versus Utf8Output](https://learn.microsoft.com/en-us/dotnet/csharp/language-reference/compiler-options/advanced#codepage).
- [QPC and high-resolution timestamp limitations](https://learn.microsoft.com/en-us/windows/win32/sysinfo/acquiring-high-resolution-time-stamps).
