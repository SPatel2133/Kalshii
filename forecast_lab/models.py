"""Probability families on an explicit integer Fahrenheit support, fitting and named fallbacks.

Support: integers lo_f..hi_f plus an underflow point (< lo_f) and an overflow point (> hi_f).
Bucket mass = sum of its support masses. All fitting uses scipy L-BFGS-B (deterministic).
"""
import math

import numpy as np
from scipy.optimize import minimize
from scipy.special import expit, log_ndtr, logsumexp

MODEL_FAMILIES = ["climatology", "market_raw", "market_recal", "market_context", "weather", "coherent",
                  "fixed_blend", "adaptive_blend", "adaptive_blend_raw", "adaptive_corrected"]


class Support:
    def __init__(self, lo_f=0, hi_f=120):
        self.lo, self.hi = int(lo_f), int(hi_f)
        self.temps = np.arange(self.lo, self.hi + 1)
        self.n = len(self.temps) + 2  # [under, lo..hi, over]

    def index_mask(self, lo, hi):
        """Boolean mask over support for inclusive integer interval [lo, hi] (None = open tail).
        Fails if a finite bound would split the unresolved underflow/overflow tail."""
        if lo is not None and not (self.lo <= lo <= self.hi + 1):
            raise ValueError(f"threshold {lo} would split an unresolved tail of the support")
        if hi is not None and not (self.lo - 1 <= hi <= self.hi):
            raise ValueError(f"threshold {hi} would split an unresolved tail of the support")
        t = self.temps
        inner = np.ones_like(t, dtype=bool)
        if lo is not None:
            inner &= t >= lo
        if hi is not None:
            inner &= t <= hi
        m = np.zeros(self.n, dtype=bool)
        m[1:-1] = inner
        m[0] = lo is None
        m[-1] = hi is None
        return m

    def bucket_matrix(self, buckets):
        A = np.stack([self.index_mask(b["lo"], b["hi"]) for b in buckets]).astype(float)
        cover = A.sum(0)
        if not np.all(cover == 1):
            raise ValueError("bucket partition is not exclusive and exhaustive on the support")
        return A

    def query(self, pmf, lo=None, hi=None):
        return float(pmf[self.index_mask(lo, hi)].sum())


def validate_probs(p, name="probabilities", tol=1e-9):
    p = np.asarray(p, dtype=float)
    if p.ndim != 1 or p.size == 0 or not np.all(np.isfinite(p)):
        raise ValueError(f"{name}: non-finite or malformed")
    if np.any(p < -tol) or abs(p.sum() - 1.0) > 1e-6:
        raise ValueError(f"{name}: not on the simplex (sum={p.sum()})")
    return np.clip(p, 0.0, None) / np.clip(p, 0.0, None).sum()


def floor_normalize(p, floor):
    p = np.maximum(np.asarray(p, dtype=float), floor)
    return p / p.sum()


# ---------------------------------------------------------------- climatology

def doy(d):
    return d.timetuple().tm_yday


def climatology_pmf(support, train_values, train_doys, target_doy, bw_days=15.0, kernel_f=3.0, eps=1e-3):
    """Training-only, strictly positive smoothed climatology for a target day of year."""
    v = np.asarray(train_values, float)
    dd = np.abs(np.asarray(train_doys, float) - target_doy)
    dd = np.minimum(dd, 365.25 - dd)
    w = np.exp(-0.5 * (dd / bw_days) ** 2)
    if v.size == 0 or w.sum() <= 0:
        p = np.ones(support.n)
        return p / p.sum()
    w = w / w.sum()
    edges_hi = np.concatenate([[support.lo - 0.5], support.temps + 0.5])
    cdf = np.zeros((v.size, edges_hi.size))
    from scipy.special import ndtr
    for i, e in enumerate(edges_hi):
        cdf[:, i] = ndtr((e - v) / kernel_f)
    mass = np.empty((v.size, support.n))
    mass[:, 0] = cdf[:, 0]
    mass[:, 1:-1] = np.diff(cdf, axis=1)
    mass[:, -1] = 1.0 - cdf[:, -1]
    p = (w[:, None] * mass).sum(0)
    p = (1 - eps) * p + eps / support.n
    return p / p.sum()


def spread_buckets_to_support(bucket_p, A, prior):
    """Distribute each bucket mass over its support points by the prior conditional within the bucket."""
    out = np.zeros(A.shape[1])
    for j in range(A.shape[0]):
        m = A[j] > 0
        c = prior[m]
        out[m] += bucket_p[j] * c / c.sum()
    return out


# ---------------------------------------------------------------- weather (Gaussian, half-degree edges)

def log_interval_mass(a, b):
    """log(Phi(b) - Phi(a)) stably for arrays a < b (use +-inf for tails)."""
    a = np.asarray(a, float)
    b = np.asarray(b, float)
    # use the upper-tail form when both are positive for precision
    flip = a > 0
    la = np.where(flip, log_ndtr(-b), log_ndtr(a))
    lb = np.where(flip, log_ndtr(-a), log_ndtr(b))
    diff = lb - la
    return lb + np.log(-np.expm1(-np.maximum(diff, 1e-300)))


def weather_design(x_nx, doys):
    ang = 2 * np.pi * np.asarray(doys, float) / 365.25
    x = np.asarray(x_nx, float)
    return np.column_stack([np.ones_like(x), x, np.cos(ang), np.sin(ang)])


class WeatherModel:
    """T ~ N(mu, sigma), mu = X @ beta, log sigma = X_s @ gamma (seasonal). Target integer T; the CLI reports
    integer degrees F so integer t occupies [t-0.5, t+0.5) — the half-degree mapping."""

    def __init__(self, beta=None, gamma=None, converged=None, n=0, nll=None, message=None, features=None):
        self.beta, self.gamma, self.converged, self.n, self.nll, self.message = beta, gamma, converged, n, nll, message
        self.features = list(features or [])

    @staticmethod
    def _sdesign(doys):
        ang = 2 * np.pi * np.asarray(doys, float) / 365.25
        return np.column_stack([np.ones_like(ang), np.cos(ang), np.sin(ang)])

    def fit(self, nx, doys, lo, hi, extras=None, names=(), l2=1e-3):
        """lo/hi: inclusive integer interval truth per event (exact value => lo == hi); None = open tail."""
        X = weather_design(nx, doys)
        names = list(names)
        if extras is None:
            if names:
                raise ValueError("weather feature names require matching extra values")
            E = np.empty((len(X), 0))
        else:
            E = np.asarray(extras, dtype=float)
            if E.ndim == 1:
                E = E.reshape(-1, 1)
            if E.shape != (len(X), len(names)):
                raise ValueError("weather extras and feature names must have matching columns")
        if E.shape[1]:
            X = np.column_stack([X, E])
        S = self._sdesign(doys)
        a = np.array([-np.inf if lower is None else lower - 0.5 for lower in lo])
        b = np.array([np.inf if h is None else h + 0.5 for h in hi])

        def nll(theta):
            split = 4 + E.shape[1]
            beta, gamma = theta[:split], theta[split:]
            mu = X @ beta
            sig = np.exp(S @ gamma)
            ll = log_interval_mass((a - mu) / sig, (b - mu) / sig)
            return -ll.sum() + l2 * (beta[2:] @ beta[2:] + gamma[1:] @ gamma[1:])

        theta0 = np.concatenate([np.array([0.0, 1.0, 0.0, 0.0]), np.zeros(E.shape[1]),
                                 np.array([math.log(3.0), 0.0, 0.0])])
        res = minimize(nll, theta0, method="L-BFGS-B", options={"maxiter": 2000, "gtol": 1e-7})
        split = 4 + E.shape[1]
        self.beta, self.gamma = res.x[:split].tolist(), res.x[split:].tolist()
        self.converged, self.n, self.nll, self.message = bool(res.success), len(a), float(res.fun), str(res.message)
        self.features = names
        return self

    def params(self, nx, d_oy, extras=None):
        X = weather_design([nx], [d_oy])
        if self.features:
            values = np.zeros((1, len(self.features))) if extras is None else np.asarray(extras, float).reshape(1, -1)
            X = np.column_stack([X, values])
        S = self._sdesign([d_oy])
        return float((X @ np.array(self.beta))[0]), float(np.exp(S @ np.array(self.gamma))[0])

    def pmf(self, support, nx, d_oy, extras=None):
        mu, sig = self.params(nx, d_oy, extras)
        edges = np.concatenate([[support.lo - 0.5], support.temps + 0.5])
        from scipy.special import ndtr
        cdf = ndtr((edges - mu) / sig)
        p = np.empty(support.n)
        p[0] = cdf[0]
        p[1:-1] = np.diff(cdf)
        p[-1] = 1.0 - cdf[-1]
        p = np.clip(p, 0, None)
        return p / p.sum()

    def state(self):
        state = {"beta": self.beta, "gamma": self.gamma, "converged": self.converged, "n": self.n,
                 "nll": self.nll, "message": self.message}
        if self.features:
            state["features"] = self.features
        return state

    @classmethod
    def from_state(cls, s):
        return cls(s["beta"], s["gamma"], s["converged"], s["n"], s["nll"], s.get("message"),
                   s.get("features", []))


# ---------------------------------------------------------------- market recalibration

def recal_probs(mid_norm, alpha, floor):
    lp = alpha * np.log(np.maximum(mid_norm, floor))
    return np.exp(lp - logsumexp(lp))


def fit_recal_scalar(mids, winners, floor):
    def nll(t):
        a = math.exp(t[0])
        return -sum(math.log(max(recal_probs(m, a, floor)[w], 1e-300)) for m, w in zip(mids, winners))
    res = minimize(nll, [0.0], method="L-BFGS-B", bounds=[(-3, 3)])
    return {"alpha": float(math.exp(res.x[0])), "converged": bool(res.success), "nll": float(res.fun), "n": len(mids)}


CONTEXT_FEATURES = ["mean_spread", "max_quote_age_h", "abs_move_hist", "min_trade_age_h"]


def market_context_x(row):
    bs = row["buckets"]
    spreads = [b["spread"] for b in bs]
    ages = [b["quote_age_h"] for b in bs]
    moves = [abs(b["mid"] - b["mid_hist"]) for b in bs if b.get("mid_hist") is not None]
    tages = [b["trade_age_h"] for b in bs if b.get("trade_age_h") is not None]
    return [float(np.mean(spreads)), float(max(ages)), float(sum(moves)) if moves else 0.0,
            float(min(tages)) if tages else 24.0]


def fit_recal_context(mids, winners, X, floor, l2):
    X = np.asarray(X, float)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd

    def nll(t):
        a = np.exp(t[0] + Z @ t[1:])
        s = 0.0
        for m, w, ai in zip(mids, winners, a):
            s -= math.log(max(recal_probs(m, ai, floor)[w], 1e-300))
        return s + l2 * float(t[1:] @ t[1:])

    res = minimize(nll, np.zeros(1 + X.shape[1]), method="L-BFGS-B", bounds=[(-3, 3)] * (1 + X.shape[1]))
    return {"theta": res.x.tolist(), "x_mean": mu.tolist(), "x_sd": sd.tolist(), "features": CONTEXT_FEATURES,
            "converged": bool(res.success), "nll": float(res.fun), "n": len(mids), "l2": l2}


def context_alpha(state, x):
    z = (np.asarray(x) - np.asarray(state["x_mean"])) / np.asarray(state["x_sd"])
    return float(np.exp(state["theta"][0] + z @ np.asarray(state["theta"][1:])))


# ---------------------------------------------------------------- coherent market model

def quote_weights(bs, age_scale_h=2.0, spread_ref=0.02):
    r = np.array([1.0 / (1.0 + b["quote_age_h"] / age_scale_h) * spread_ref / (spread_ref + b["spread"]) for b in bs])
    return r / r.mean()


def coherent_solve(prior_b, bid, ask, r, lam, tol=1e-10, maxiter=500):
    """Minimize KL(m || c) + lam * sum r_j dist(m_j, [bid_j, ask_j])^2 over the simplex (bucket space).

    Parametrized as m = softmax(z); deterministic L-BFGS-B. Returns (m, diagnostics). On non-convergence or
    non-finite output, returns the documented fallback: normalized midpoints clipped into the simplex."""
    c = np.asarray(prior_b, float)
    bid, ask, r = map(lambda v: np.asarray(v, float), (bid, ask, r))
    logc = np.log(c)

    def f(z):
        lm = z - logsumexp(z)
        m = np.exp(lm)
        kl = float(m @ (lm - logc))
        under = np.maximum(bid - m, 0)
        over = np.maximum(m - ask, 0)
        d = under + over
        pen = lam * float(r @ (d * d))
        # gradient wrt m
        gm = (lm - logc + 1.0) + lam * 2 * r * (over - under)
        g = m * (gm - m @ gm)
        return kl + pen, g

    z0 = np.log(np.clip((bid + ask) / 2, 1e-6, None))
    z0 -= logsumexp(z0)
    res = minimize(f, z0, jac=True, method="L-BFGS-B", options={"maxiter": maxiter, "gtol": tol, "ftol": 1e-14})
    m = np.exp(res.x - logsumexp(res.x))
    resid = np.where(m < bid, m - bid, np.where(m > ask, m - ask, 0.0))
    diag = {"converged": bool(res.success), "iterations": int(res.nit), "objective": float(res.fun),
            "residuals": resid.tolist(), "max_abs_residual": float(np.abs(resid).max()),
            "sum_bid": float(bid.sum()), "sum_ask": float(ask.sum()),
            "infeasible_book": bool(bid.sum() > 1 + 1e-12 or ask.sum() < 1 - 1e-12), "lambda": lam,
            "fallback": None}
    if not res.success or not np.all(np.isfinite(m)):
        mid = (bid + ask) / 2
        m = mid / mid.sum()
        diag["fallback"] = "coherent_nonconvergence_normalized_mid"
    return m, diag


# ---------------------------------------------------------------- blends

def fit_fixed_blend(pm_win, pw_win):
    """Constant w in [0,1] minimizing -sum log(w*pm + (1-w)*pw) at the winning interval."""
    pm_win, pw_win = np.asarray(pm_win), np.asarray(pw_win)

    def nll(w):
        return -np.log(np.maximum(w[0] * pm_win + (1 - w[0]) * pw_win, 1e-300)).sum()

    res = minimize(nll, [0.5], method="L-BFGS-B", bounds=[(0.0, 1.0)])
    return {"w": float(res.x[0]), "converged": bool(res.success), "nll": float(res.fun), "n": int(len(pm_win))}


GATE_FEATURES = ["disagreement_js", "mean_spread", "max_quote_age_h", "weather_sigma"]


def js_divergence(p, q):
    p, q = np.asarray(p) + 1e-12, np.asarray(q) + 1e-12
    p, q = p / p.sum(), q / q.sum()
    m = (p + q) / 2
    return float(0.5 * (p @ np.log(p / m)) + 0.5 * (q @ np.log(q / m)))


def fit_gate(pm_win, pw_win, X, l2):
    pm_win, pw_win, X = np.asarray(pm_win), np.asarray(pw_win), np.asarray(X, float)
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd

    def nll(t):
        g = expit(t[0] + Z @ t[1:])
        p = g * pm_win + (1 - g) * pw_win
        return -np.log(np.maximum(p, 1e-300)).sum() + l2 * float(t[1:] @ t[1:])

    res = minimize(nll, np.zeros(1 + X.shape[1]), method="L-BFGS-B")
    return {"theta": res.x.tolist(), "x_mean": mu.tolist(), "x_sd": sd.tolist(), "features": GATE_FEATURES,
            "l2": l2, "converged": bool(res.success), "nll": float(res.fun), "n": int(len(pm_win))}


def gate_value(state, x):
    z = (np.asarray(x, float) - np.asarray(state["x_mean"])) / np.asarray(state["x_sd"])
    return float(expit(state["theta"][0] + z @ np.asarray(state["theta"][1:])))


# ---------------------------------------------------------------- bounded distribution correction

def corrected_pmf(pmf, support, a1, a2, bound=1.0):
    """log p'(t) = log p(t) + a1*z + a2*z^2, z = (t - mean)/sd of p; normalized. a's bounded to [-bound, bound]."""
    t = np.concatenate([[support.lo - 1], support.temps, [support.hi + 1]]).astype(float)
    mean = float(pmf @ t)
    sd = math.sqrt(max(float(pmf @ (t - mean) ** 2), 1e-6))
    z = (t - mean) / sd
    a1, a2 = float(np.clip(a1, -bound, bound)), float(np.clip(a2, -bound, bound))
    lp = np.log(np.maximum(pmf, 1e-300)) + a1 * z + a2 * z * z
    return np.exp(lp - logsumexp(lp))


def fit_correction(pmfs, win_masks, support, bound=1.0):
    def nll(a):
        s = 0.0
        for p, m in zip(pmfs, win_masks):
            s -= math.log(max(corrected_pmf(p, support, a[0], a[1], bound)[m].sum(), 1e-300))
        return s

    res = minimize(nll, [0.0, 0.0], method="L-BFGS-B", bounds=[(-bound, bound)] * 2)
    return {"a1": float(res.x[0]), "a2": float(res.x[1]), "converged": bool(res.success), "nll": float(res.fun),
            "n": len(pmfs), "bound": bound}
