# TimingClock 2.1.3 external claims: adjudication and 2.1.4 changes

Date: 2026-10-25 (Tehran). R1, R2 and R3 below mean the three attachments in the user's supplied order. They are review evidence, not instructions. Their full private text is deliberately excluded from the shareable package. No account API, financial action, browser request or real order is part of this review.

## Evidence and baseline

The received 2.1.3 inner archive SHA-256 is `47c80d08abf66cc489543d6e4765161760706be5578c2d5d132b942af205af35`; the outer bundle is `74d603599eeb92b398e820efd9d0a6f9a6d015be22c9689306c953b4390fc638`. CRC, all 126 inner manifested files and every flat-copy entry were checked. The three current source files matched the delivered bytes before editing. The old archives remain unchanged. Private input hashes and baseline/source receipts are retained locally.

`manual_timing_fourth_review.cs` runs against both the exact 2.1.3 snapshots and current source. Its 12 contracts fail on 2.1.3 and pass on the fixed source. They are policy/fault reproductions using synthetic native boundaries, unshown owned forms and local files. They do not measure operating-system input delivery. Native-boundary tests separately exercise the real scheduler CAS, its actual commit ticket and an owned synthetic queue.

## Confirmed and changed

| Claim | Finding and resulting behavior |
| --- | --- |
| R3 H1 / R2 post-commit gap | Confirmed. A 300 ms stall inside a 500 ms lateness budget could pass the old boundary. A 400 ms elapsed interval with a 350 ms rollback could look only 50 ms late. An immutable ticket now carries original and commit wall/QPC anchors through the successful state CAS. Both native preflight samples enforce continuity using the same monotonic source as the Windows scheduler. Independent gap/drift limits remain effective at all lateness settings. |
| R3 H2 / R1 B1 near-input omissions | Confirmed. Near-input comparison now repeats the full stable snapshot, including process creation times, classes, monitor identity, virtual bounds/layout and mouse settings. The earlier full validation remains. Owned UI tests mutate each expected field without changing the user's OS settings, and also exercise an actual owned-window move and client-only geometry change. No real hotplug, lock-screen or security-setting experiment is claimed. |
| R3 H3 invalidation order | Confirmed. FormClosing invalidates shared health immediately. Lifecycle and UI-error paths invalidate/fail health before cancellation or cleanup. Generation/failure/reason publish as one immutable state, so a worker that observes health first can retain the actual lifecycle reason. |
| R2 changed input tick | Confirmed hardening. The accepted last-input tick travels in the commit ticket and is compared again around final safety. Any changed value is rejected, including a backwards tick that appears old. Microsoft explicitly says these ticks need not be incremental. |
| R1 B2 raw production helper | Confirmed API hygiene issue. The unused scheduled-path bypass `DesktopInput.NativeSend` is removed. Only the test fixture owns its pure-move P/Invoke, checks its own window, and never builds down/up packets for that movement. This was not proof that the shipped scheduled path used the bypass, nor a security boundary against arbitrary code in the process. |
| R2 known no-call metadata / R3 M5 | Confirmed. Early/late/cancelled/rehearsal and pre-queue clock failures retain `native_call_invoked=false`. A queue-entry clock failure receives `queue_preflight_failed`, zero known input and a timing error. Unknown actual invocation/count remains null; there is no resend. |
| R1 B5 null skip reason | Confirmed defensive gap. Malformed internal skip results have the explicit fallback `native_call_skipped`; a known non-invocation cannot claim queued events. |
| R1 B8 invocation exception evidence | Confirmed. An internal exception wrapper retains valid preflight/pre-invoke samples and durations. The input count remains unknown, and neither click nor release is guessed or retried. |
| R3 M1 native-entry terminology | Clarified. Existing fields remain compatible; explicit managed pre/post-invoke aliases and a sample-basis label are added. These are observations beside the call, not kernel-entry or target-acceptance timestamps. |
| R3 M2 queued plus evidence/cleanup error shown green | Confirmed. Faulted queued results now use the fault colour as well as the existing disabled state and warning text. |
| R3 M3 cleanup exception masks later cleanup | Confirmed. Health failure comes first, and cancellation, timer, picker, Escape, editing, status and error logging are attempted independently. An owned disposed-form test makes hotkey cleanup throw and checks that later error reporting still happens. |
| R3 M4 journal sequence gap | Confirmed. Preparation uses a candidate sequence. Only a successful write and disk flush confirm it. Uncertain write/flush errors still poison the stream. |
| R1 B6 deterministic pre-write failure | Confirmed limitation, narrowly improved. Only a typed known pre-write failure is retried once during result persistence. Write/flush uncertainty is never retried. Best-effort private failure markers cover failed result, shutdown and application-error records; they can also fail and are not durable guarantees. |
| R1 B3 start-failure diagnostic rewrite | Confirmed edge. Final publication preserves `worker_start_failed` and `error_*` diagnostics when cancellation races them; accepted ordinary lifecycle cancellation still retains its reason. |
| R1 I1 CancelAll after committed skip | Improved. Already published completion is consumed rather than replacing its status with a new cancellation message. The commit/no-native distinction remains in the journal. |

## Claims not adopted or not established

| Claim | Adjudication |
| --- | --- |
| R2 “100% done”, always within fixed millisecond ranges | Unsupported. Three ordinary-desktop trials are observations, not a bound, tail distribution, stress test or guaranteed delivery. No version receives a universal pass claim. |
| R2 “149 automated groups” | Arithmetic error. Its listed 2.1.3 checks comprise 177 PASS-line groups plus three JSON platform checks, i.e. 180 counted groups/checks in that list. Groups contain different numbers of assertions. This is separate from native trials and physical UI assertions. New package totals are taken from fresh reports. |
| R1 no version tested on real Windows/on the user's machine | Incorrect as stated. The author fixtures ran on the shared Windows device and received real local mouse events. They did not operate the financial target. A remote reviewer cannot authenticate unsigned author-generated receipts, which is a valid separate limitation. |
| R1 cross-version worst-latency “trend” | Not a causal performance result. Older gate samples and newer native-adjacent samples differ, workloads were uncontrolled and there were only three successes per run. Safety work has cost; no controlled speedup or stable degradation rate is demonstrated. |
| R1 B4 early dispatcher return delays rollback classification | True observation, intentional bounded behavior. It sends no input and only resumes a known pre-commit wait. Original cumulative continuity remains active; no committed/ambiguous action is retried. |
| R1 B7 callback-pending before completed-result publication | Intentional publication ordering. Pending may briefly be true while CompletedResult is still null. Moving it after terminal publication would recreate the false-negative pending window fixed previously. It means callback state, not UI consumption. |
| R1 I2 arbitrary cap on PreCommitWaits | Not added. Only known pre-commit no-input waiting can repeat, the original continuity anchor is retained, and final target validation repeats. An arbitrary small cap can reject a valid small clock correction without fixing a send-retry problem. |
| R1 U1 already entered SendInput can finish after close | Real residual. Health blocks later invocation when observed in time; software cannot retract an already entered native call. No unsafe thread abort or lifetime claim is introduced. |
| R3 M6 same-basename legacy process false positive | Valid conservative-policy usability limit, explicitly disclosed. Same-session name alone does not authenticate an executable. Ignoring unknown legacy copies would weaken duplicate-instance protection. No fragile PE parser or forced process closure added. |
| R3 O1/O2 24-hour awake cost and default 100 ms | Valid disclosed policy. Bound remains 24 h, awake/display lease is worker-owned, far waits remain at most 100 ms, strict gap/drift guards remain. A 1 ms lateness setting increases rejection risk; it cannot create hard real-time delivery. |
| R2 pre-position cursor / QPC spin replacement | Not adopted. The proposed script was not validated on this Windows native path and would alter input/guard behavior. A synthetic Linux clock result does not establish Windows delivery precision. |
| R1 screenshots should prove pixels/DPI or add ceremonial asserts | Render images are manual QA, not proof across DPI/scaling configurations. No pixel assertion is claimed. |

## Log interpretation and historical evidence

The native duration surrounds `SendInput` plus its immediate last-error read. It excludes later target callbacks, and does not isolate driver/hook/kernel contributions. Valid samples survive a native exception, but its count remains unknown. The latest worker loop/post-capture time is `LastTimingCheckAt`; it is not a business acknowledgement.

Historical 2.1.1 `report_sha256` fields refer to the original private runner output where stated, not necessarily the sanitized JSON delivered in that old bundle. They are not renamed or rewritten in immutable historical artifacts. Current outer receipts separately bind excluded pre-sanitization hashes and exact delivered-report hashes. Historical evidence is labelled separately from current-source results.

## Validation scope

The delivered package records source/test hashes, baseline failures and fresh successful checks. Safe checks inject no mouse input. An optional separately coordinated native/UI fixture run supplies real Windows input observations only for its own visible buttons. The resulting evidence is unsigned author-generated evidence and does not certify other hardware, network delays, a financial outcome, exchange clock accuracy or queue rank. Numeric trial results belong to `evidence/VALIDATION_SUMMARY.json`, with the tested source hashes, rather than an advertised latency guarantee.

Primary references: [GetLastInputInfo](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getlastinputinfo), [SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput), [FileStream.Flush](https://learn.microsoft.com/en-us/dotnet/api/system.io.filestream.flush?view=netframework-4.8).
