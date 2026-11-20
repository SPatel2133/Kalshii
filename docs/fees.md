# Kalshi trading fees used by the v3 paper backtest

**Source (official):** Kalshi Fee Schedule, "Fee Schedule for July 2026 - 7.7.26 Update",
https://kalshi.com/docs/kalshi-fee-schedule.pdf (linked from https://help.kalshi.com/en/articles/13823805-fees).
Retrieved 2026-11-19 (UTC), sha256 `c326a69f596a11e8f8be2620402d39a8d4823920c21cc97c93a114d862699601`. "Last updated and effective: July 7, 2026".
The PDF is not committed (redistribution terms unclear); the hash and the quoted text below are the record.

**Taker (general) fee, quoted:** `fees = round up(M x 0.07 x C x P x (1-P))`, P = contract price in dollars, C = contracts,
M = multiplier (default 1). Fees are charged only on orders that immediately match resting orders.

**Maker fee, quoted:** `fees = round up(M x 0.0175 x C x P x (1-P))`, M default 0. The backtest only simulates immediately
filled (taker) orders at the as-of bid/ask, so maker fees do not apply.

**Weather series multiplier:** none of KXHIGHNY/CHI/MIA/AUS/LAX/DEN/PHIL appears in the schedule's "Non-Standard Fees" table.
The public API `GET /trade-api/v2/series/<ticker>` returns `fee_type = "quadratic"`, `fee_multiplier = 1` for all seven, and
`GET /series/fee_changes?show_historical=true` lists no fee changes for any KXHIGH series (checked 2026-11-19).

**Rounding:** the schedule text says "rounds up such that the fee + positionCost is rounded to a centicent", but the schedule's own
General Trading Fees Table rounds up to the whole cent (1 contract: $0.01 at P = 0.01-0.15 and 0.85-0.99, $0.02 at 0.20-0.80;
100 contracts: $0.07 at 0.01, $0.34 at 0.05, $1.75 at 0.50). We use the table convention, ceil to the cent:
`fee_cents = ceil(0.07 x M x C x p x (100 - p) / 100)` with p in cents. Centicent rounding could only lower fees, so this is the
conservative choice for a profitability test.

**Assumptions / limitations:** the July 2026 schedule is applied to the whole history; earlier schedules were not retrieved, and
the API lists no KXHIGH fee changes. Sensitivities: 0x and 2x fees, and +1 cent slippage per contract.
