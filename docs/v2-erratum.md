# Erratum: nyc-0800-v2 local-day window

**Finding.** NWS Directive 10-1004 (https://www.weather.gov/media/directives/010_pdfs/pd01010004curr.pdf, retrieved 2026-11-19,
sha256 `4d26e5f914a9062274aa3d3cfe8473448c20a9136645c632568e3307cbdb5ebe`), §4.1.2: the first daily CLI issuance captures "the previous
calendar day's (midnight-to-midnight Local Standard Time [LST]) data"; the CF6 key likewise says "All data are for midnight to
midnight LST". The climate day is therefore 05Z-05Z for New York all year (01:00-01:00 EDT during daylight saving time).

v2 used a DST-aware local-midnight window (04Z-04Z during EDT) for `sampled_max_local_f` and as the start of the morning
observation window (`obs_max_so_far_f`, `obs_latest_f`, `n_obs`). v1 is not affected (v1's sampled proxy already used 05Z-05Z and v1
has no observation features).

**Impact on v2 data** (recomputed from `data/work-v2/dataset` and the cached MOS/ASOS rows; the selected MOS run is unchanged):

| Split | Events (scored) | DST-period events (scored) | `sampled_max_local_f` changed | `obs_max_so_far_f` changed (scored) | `n_obs` changed (scored) |
|---|---|---|---|---|---|
| dev | 1,034 (811) | 681 (536) | 0 | 294 (223) | 679 (534) |
| test | 287 (287) | 161 (161) | 0 | 76 (76) | 161 (161) |
| audit | 30 (30) | 30 (30) | 0 | 11 (11) | 30 (30) |

"Scored" = no primary exclusion reason in the v2 coverage funnel. The sampled maximum never changes because GFS MOS `tmp` is
3-hourly at 00/03/.../21Z, so both windows contain the same 06Z-03Z samples. The observation maximum changes when an
observation between 04Z and 05Z (00:00-01:00 EDT) was the warmest of the morning.

**Disposition.** v2 is not refit or rescored; its reported numbers stand as computed under the documented (incorrect) window.
v3 uses the LST window everywhere (`[weather] day_window = "lst"`). Computation: `scripts/v2_erratum.py` (recomputes these
counts from the local cache).
