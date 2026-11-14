# Timing clock 2.0.1: deadline follow-up

Date: 2026-10-24, Asia/Tehran. This supplements the [2.0.0 engineering review](2026-10-24-timing-clock-hardening.md). Scope: the generic local desktop scheduler. No financial action was armed or submitted by this review.

## Reproduced issue and fix

A new deterministic test demonstrated a missed continuity check: if final target validation blocked for 500 ms and the wall clock moved backwards by 450 ms during that call, the apparent wall-clock lateness was only 50 ms. Version 2.0.0 could dispatch inside its 100 ms window despite the long monotonic gap. The failing report is preserved at `tests/.timing-output/deadlines-f0b5a793/report.json`; its manifest binds the source used for that test.

Version 2.0.1 checks wall-clock and monotonic time again after target, input and UI validation, immediately before committing to dispatch. It checks the time window, the duration of that validation interval and cumulative drift since the worker started. The reproduced case now cancels. This adds cheap clock reads without removing any target guard or widening any limit.

Eight deadline regressions pass: the exact 1 ms software-dispatch boundary, one 100 ns timestamp tick beyond it, a target check that consumes the allowed window, backward clock movement, a gap masked by a clock correction, cumulative drift, a long guard stall inside a wide lateness window, and a valid short guard delay. They use simulated clocks and a simulated sink confined to tests; they send no desktop input. The existing 26 clock checks, all 32 fault groups, and the UI workflow in no-input mode also pass on 2.0.1.

## The timing requirement this does and does not meet

`HH:mm:ss.fff` expresses the desired time in steps of 1 ms. The configured lateness limit is a check on a sampled software-dispatch timestamp. It is not a reservation of CPU time, a deadline attached to the Windows input queue, or a guarantee that the destination processes a click in that interval. The default limit remains 100 ms. Choosing 1 ms makes detected misses cancel more often; it does not make Windows deliver input within 1 ms.

There is always a residual interval between the last clock/target check and the native operation. The process can be preempted there. Input already queued cannot be withdrawn. Destination event handling and network/broker/exchange processing add separate delays. Consequently, neither "always click" nor "always deliver within 1 ms" is proven; a late or invalid plan deliberately cancels rather than being retried.

After the operator confirmed an idle desktop, fresh native input tests passed on 2.0.1. Each of three positive trials produced exactly one mouse-down, one mouse-up and one click in the owned local fixture. Software-dispatch lateness was 4.6821, 0.6043 and 0.5645 ms; destination mouse-down lateness was 25.2647, 14.9662 and 10.5792 ms. The six cancellation, overlay/target-change, movement, resize, disabled-target and recent-input trials each produced zero scheduled input. Receipts and the manifest binding all three production source hashes are in `tests/.timing-output/manual-clock-click-1b21f49485264577b8e429ad85ee9b75/`.

These are three local observations, not a percentile estimate, a worst-case bound or broker/exchange evidence. They do not establish that delivery will occur within 1 ms. The three native observations in the previous review remain evidence for 2.0.0, with separate receipts; they are not relabelled as current-version results.

Microsoft documents [nondeterministic completion times on a normal operating system](https://learn.microsoft.com/en-us/windows/iot/iot-enterprise/soft-real-time/soft-real-time) and describes even its configured Windows IoT real-time feature as allowing jitter. [SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput) reports input events inserted into the input stream; its return value does not establish destination processing time. [GetSystemTimePreciseAsFileTime](https://learn.microsoft.com/en-us/windows/win32/api/sysinfoapi/nf-sysinfoapi-getsystemtimepreciseasfiletime) provides a precise local time-of-day reading, not an end-to-end delivery guarantee.

Reproduce the added regressions without mouse input:

```powershell
powershell.exe -NoProfile -File .\tests\run_manual_timing_deadlines.ps1
```
