# TOLL COLD-PATH PERFORMANCE REPORT

Measured on 2026-09-12 (Asia/Seoul), Windows, local FastAPI/OSRM deployment. Values below are measured observations, not estimates.

## 1. Baseline

- aggregate latency: approximately 13.9–14.1 s before the spatial-index and cold-path work
- toll matching: approximately 6.7 s before the spatial index; approximately 43 ms after the index in the focused measurement
- cache hit: not consistently pair-level; per-vehicle/candidate work could still reach the request path
- cold official lookup: Playwright was previously the primary cold path; no stable HTTP-primary baseline was available
- Playwright startup: previously paid on fallback requests; now shared and warmed/reused

## 2. Bottleneck Breakdown

Focused before/after measurements:

| Stage | Measured result |
|---|---:|
| OSRM route | 123–172 ms in the representative matrix |
| TG spatial matching | approximately 43 ms focused; 11–45 ms in live diagnostics |
| TG deduplication | included in the TG matching timing; no longer an all-segment scan |
| station resolution | local verified dictionary/cache path; typically low single-digit to tens of ms |
| toll cache | direct batch lookup approximately 0.6–6.5 ms |
| cold direct HTTP | 566.99 ms for 동서울 → 대동 |
| warm Playwright browser reuse | launch/reuse 0 ms; fallback lookup total 759.38 ms after warm-up |
| HTML parser | 13.06 ms direct HTTP; 30.49 ms Playwright |
| DB cache write | included in source lookup; no observed DB lock regression |
| aggregate | warm p50 215.46–454.48 ms by representative route; cold full aggregate remains variable because route orchestration and reverse lookup can overlap/contend with official I/O |

The largest former CPU bottleneck was removed with the uniform route-segment spatial index. The remaining cold-path variability is external official-site I/O plus candidate/source orchestration, not tollgate projection CPU.

## 3. HTTP Primary

- request flow: verified robots policy → official initial GET → observed `pathCheckN.do` station mapping POST when needed → official result POST → validated result DOM parsing
- connection reuse: one persistent `httpx.AsyncClient`, keep-alive enabled, connection limits capped at 2
- validation: HTTP status, content type, allowed final hostname, requested entry/exit, complete route result, all vehicle prices, and official distance
- parity tests: 5 representative pairs passed; HTTP and Playwright matched IDs, route metadata, distance, and all six prices
- supported response: complete official result table only; partial/no-route/parse-invalid responses remain unavailable and are never converted to zero
- direct cold measurement for 동서울 → 대동: 566.99 ms, 4 official HTTP requests, exact price table returned

The direct cold HTTP breakdown was: robots 116.48 ms, initial GET 76.52 ms, station mapping 47.66 ms, result POST 162.63 ms, and DOM parsing 13.06 ms.

## 4. Playwright Fallback

- browser pool: shared Chromium instance with a small lifecycle-managed browser/context design
- process reuse: fallback benchmark reported `browser_launch_or_reuse_ms = 0`; warm-up was 266.48 ms and was paid once
- fallback triggers: timeout, unexpected/changed HTML, or parser mismatch where browser fallback is safe; explicit access-policy/unknown-station/no-complete-route cases are not blindly retried in a browser
- latency: 1,026.04 ms including warm-up in the standalone fallback benchmark; lookup itself was 759.38 ms
- isolation: fresh context per lookup prevents entry/exit, result DOM, hidden fields, and cookies from contaminating the next lookup

## 5. Cache Redesign

- pair key: `(entry_official_id, exit_official_id)` with direction preserved
- full vehicle table: class 1, 2, 3, 4, 5, and compact/경차 are stored together in the existing `toll_rates_cache` schema
- TTL: fresh for 30 days
- stale window: verified stale values are usable for up to 180 days; older values are treated as missing
- indexes: composite `(entry_official_id, exit_official_id, fetched_at)` index plus existing expiry/index support
- metadata: official distance, route metadata, fetched/expiry timestamps, parser version, source status
- vehicle-class changes read the cached table and do not issue another official request
- stale values are labeled `STALE_CACHE` and retain their last official confirmation time; they are not presented as current

## 6. Prefetch

- outbound: after a stable route is ready, the frontend starts the aggregate/toll work after a 250 ms debounce while route information is already rendered
- reverse: outbound official identity triggers a lower-priority reverse-pair prefetch when the return route is available
- cancellation: `AbortController`, request generation, route version, route identity, and pair identity prevent obsolete responses from updating current state
- stale-response protection: an old 부산 → 서울 lookup cannot overwrite a newer 부산 → 강릉 selection
- UI: prepared route/fuel information can be shown while the official toll state remains “공식 유가와 통행료 확인 중”

## 7. Single-flight

- key: `(entry_station_id, exit_station_id)`
- concurrent test: duplicate concurrent requests share one in-flight future and receive the same result/exception
- official request count: one source lookup for duplicate pair requests; in-flight entries are removed on both success and failure
- failure behavior: waiters receive the same failure and do not remain blocked; short failure cooldown prevents repeated slow retries for known unavailable directions

## 8. Representative Benchmark

Warm matrix, 5 repetitions per route. `p50` and `p95` are measured request latencies; all reported warm toll paths were `CACHE`.

서울 → 부산:
- warm: p50 454.48 ms, p95 482.98 ms
- cold HTTP: direct pair benchmark 566.99 ms (동서울 → 대동 representative official pair)
- browser fallback: 1,026.04 ms standalone, including one-time warm-up

서울 → 대전:
- warm: p50 248.76 ms, p95 3,851.30 ms
- cold HTTP: not separately cleared for this route in the final matrix
- browser fallback: not separately measured for this route
- note: p95 contains the first unavailable reverse-direction attempt before the failure cooldown; subsequent repetitions were approximately 247–267 ms

부산 → 대구:
- warm: p50 215.46 ms, p95 546.27 ms
- cold HTTP: not separately cleared for this route in the final matrix
- browser fallback: not separately measured for this route

강릉 → 서울:
- warm: p50 323.75 ms, p95 339.07 ms
- cold HTTP: not separately cleared for this route in the final matrix
- browser fallback: not separately measured for this route

대전 → 광주:
- warm: p50 253.49 ms, p95 269.33 ms
- cold HTTP: not separately cleared for this route in the final matrix
- browser fallback: not separately measured for this route

An exact full aggregate cold run after clearing pair `190 → 252` measured 2,741.73 ms. The selected outbound official HTTP result itself was 109.62 ms, while route candidate/source orchestration reported 2,589.05 ms for the toll lookup stage. This is the main remaining cold aggregate bottleneck and is reported separately from the direct HTTP client budget.

## 9. Before / After

| Metric | Before | After |
|---|---:|---:|
| TG match | approximately 6,757.9 ms | approximately 43 ms focused; 11–45 ms live |
| Warm aggregate | approximately 13.9–14.1 s | representative p50 215.46–454.48 ms |
| Cold HTTP | Playwright-primary/unbounded baseline | 566.99 ms direct official pair |
| Browser fallback | Chromium launch paid on request | 1,026.04 ms including one-time warm-up; reuse launch cost 0 ms |

## 10. Correctness Regression

- official toll exactness: HTTP vs Playwright parity passed for all five representative pairs
- vehicle classes: class 1–5 and compact prices matched exactly
- direction: directional pair key is preserved; A → B is never reused for B → A
- private/partial: incomplete or invalid official route results remain unavailable
- unknown != 0: no amount is synthesized; unavailable tolls remain “확인 불가”
- distance sanity: a cached/source result with an incompatible route distance is rejected rather than reused

## 11. Tests

- Python: `178 passed, 2 warnings` with `python -m pytest -q`
- non-integration Python: `154 passed, 24 deselected`
- integration route/OSRM: `16 passed`
- official toll integration: `1 passed`
- official fuel integration: `2 passed` (included in the full run)
- parity: 5/5 representative pairs, all prices/distance/route metadata equal
- Node: 6 passed; `static/js/cost.js` syntax check passed
- Chromium E2E: PASS; 115 requests, 0 unapproved/forbidden external requests, 0 console errors

## 12. Network Audit

- official hosts: configured localhost services, OpenFreeMap, Korean Expressway Corporation official host, and Opinet official host
- prohibited hosts: no new proxy or third-party data host added
- unexpected requests: browser smoke test reported `external_requests=[]` and `forbidden_requests=[]`
- redirect policy: HTTP and browser paths validate the final hostname against the official allowlist
- logging: request diagnostics retain timings/status/source path without dumping full cookies or sensitive session data

## 13. Issues Found

- all-segment tollgate projection caused the original multi-second CPU bottleneck
- the prior browser-primary cold path paid browser startup and navigation latency
- per-vehicle cache behavior could repeat official lookup after vehicle changes
- station directory/mapping work could enter the request hot path
- stale diagnostic/request metadata could be overwritten by concurrent reverse prefetch
- a valid cached result with an incompatible route distance could be incorrectly considered
- unavailable station/partial-route cases could trigger unnecessary browser attempts
- a database build can fail with Windows `WinError 32` when another process holds `.building`; this is an external file-lock condition, not a toll-price fallback

## 14. Issues Fixed

- added spatial route-segment index and preserved TG matching correctness
- made verified HTTP the primary official lookup and retained Playwright as a validated fallback
- added persistent HTTP connection reuse, host/redirect validation, and detailed parser validation
- upgraded toll caching to directional official pair/full-vehicle-table semantics
- added fresh/stale/missing states, bounded stale window, refresh tracking, and failure cooldown
- persisted and reused the 455-station official dictionary; added verified aliases and local candidate filtering
- added outbound/reverse speculative prefetch, keyed single-flight, cancellation, and stale-response protection
- added browser reuse/warm-up and fresh-context isolation
- added structured timing/source-path diagnostics, parity/benchmark tools, a safe exact-pair cache-clear utility, and regression tests
- fixed route/fuel/toll concurrency and round-trip integrity regressions without changing unknown/zero semantics

## 15. Remaining Bottlenecks

- The official website remains an external cold dependency; direct HTTP is within the 0.5–1.5 s target in the measured pair, but the full aggregate can still be slower when candidate validation, reverse prefetch, or an unavailable direction is involved.
- The current API still returns one completed aggregate response rather than a true SSE/partial response. The frontend hides much of this with route-ready speculative prefetch and progressive toll status.
- Official no-route/partial results correctly remain unavailable; they cannot be made faster by inventing a toll.
- The warm aggregate has a route/OSRM floor of roughly 0.12–0.17 s plus matching and application overhead, which is now materially below the former toll-matching bottleneck.

## 16. Final Verdict

PARTIAL

The requested correctness-preserving architecture is implemented and verified: HTTP is primary, Playwright is reusable fallback, pair/full-table caching, stale semantics, prefetch, reverse prefetch, single-flight, station dictionary caching, exactness checks, and network safeguards are in place. Warm requests meet the practical sub-second target in the representative matrix, and direct cold HTTP meets the 0.5–1.5 s target.

It is marked `PARTIAL` rather than `PASS` because the measured full aggregate cold path still has a 2.74 s outlier caused by route-level candidate/source orchestration and reverse/unavailable-direction behavior. No inaccurate estimate or zero-value substitution was introduced to hide that latency.
