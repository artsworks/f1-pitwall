"""Monte Carlo agreement for deterministic pit-window plans."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace

import numpy as np
from numpy.typing import NDArray

from pitwall.model.deg import DegFit
from pitwall.strategy.pitwindow import (
    PitPlan,
    _candidate_range,
    _cliff_age,
    _f,
    _fresh_fit,
    _lap_ms,
)


@dataclass(frozen=True, slots=True)
class McResult:
    modal_lap: int
    agree: float
    sims: int
    q_modal_lap: int = 0


def _sampled_current_laps_ms(
    *,
    fit: DegFit,
    tyre_age: int,
    ages: NDArray[np.float64],
    cur_cliff_age: float,
    deg_samples: np.ndarray,
    th: Mapping[str, object],
) -> NDArray[np.float64]:
    current_ages = tyre_age + ages[None, :]
    return (
        fit.base_ms
        + deg_samples[:, None] * current_ages
        + fit.deg2_ms_per_lap2 * current_ages**2
        + _f(th, "cliff_ms_per_lap", 1000) * np.maximum(current_ages - cur_cliff_age, 0.0)
    )


def _candidate_deltas_s(
    *,
    fit: DegFit,
    fresh_fit: DegFit,
    laps_remaining: int,
    tyre_age: int,
    cur_cliff_age: float,
    deg_samples: np.ndarray,
    pit_losses_s: np.ndarray,
    sc_mask: np.ndarray,
    sc_pit_loss_s: float,
    last_k: int,
    th: Mapping[str, object],
) -> NDArray[np.float64]:
    ages = np.arange(laps_remaining, dtype=float)
    cliff_penalty = _f(th, "cliff_ms_per_lap", 1000)
    current_laps_ms = _sampled_current_laps_ms(
        fit=fit,
        tyre_age=tyre_age,
        ages=ages,
        cur_cliff_age=cur_cliff_age,
        deg_samples=deg_samples,
        th=th,
    )
    current_prefix_ms = np.concatenate(
        (np.zeros((len(deg_samples), 1)), np.cumsum(current_laps_ms, axis=1)), axis=1
    )
    fresh_laps_ms = np.array(
        [
            _lap_ms(
                fresh_fit,
                age,
                _cliff_age(fresh_fit, _f(th, "tyre_cliff_ms", 1500)),
                cliff_penalty,
            )
            for age in range(laps_remaining)
        ],
        dtype=float,
    )
    fresh_prefix_ms = np.concatenate(([0.0], np.cumsum(fresh_laps_ms)))
    ks = np.arange(last_k + 1)
    pit_costs_s = np.where(sc_mask, sc_pit_loss_s, pit_losses_s[:, None])
    deltas_ms = (
        current_prefix_ms[:, ks]
        + fresh_prefix_ms[laps_remaining - ks][None, :]
        + (pit_costs_s + _f(th, "outlap_warmup_s", 0.8)) * 1000.0
        - current_prefix_ms[:, laps_remaining, None]
    )
    return deltas_ms / 1000.0


def plan_agreement(
    *,
    lap_num: int,
    laps_remaining: int,
    tyre_age: int,
    fit: DegFit,
    fresh: DegFit,
    cur_cliff_age: float,
    pit_loss_s: float,
    pit_loss_samples_s: Sequence[float],
    sc_pit_loss_s: float,
    plan_lap: int,
    mode: Mapping[str, object],
    th: Mapping[str, object],
    rng: np.random.Generator,
    plan_name: str = "",
    cover_gap_s: float = math.inf,
) -> McResult | None:
    sims = int(_f(th, "plan_mc_sims", 500))
    min_left, last_k = _candidate_range(laps_remaining, th)
    if sims <= 0 or laps_remaining <= min_left or last_k < 0:
        return None

    fresh_fit = _fresh_fit(fit, fresh)
    sigma = max(
        0.0,
        fit.deg_sd_ms_per_lap,
        _f(th, "plan_mc_deg_sd_frac", 0.15) * fit.deg_ms_per_lap,
    )
    if fit.deg_posterior_sd_ms_per_lap > 0 and fit.n >= max(int(_f(th, "deg_min_laps", 3)), 3):
        scale = max(fit.deg_posterior_sd_ms_per_lap, sigma)
        deg_samples = np.maximum(
            fit.deg_ms_per_lap + rng.standard_t(df=max(fit.n - 2, 3), size=sims) * scale,
            0.0,
        )
    else:
        deg_samples = np.maximum(rng.normal(fit.deg_ms_per_lap, sigma, size=sims), 0.0)
    if len(pit_loss_samples_s) >= 3:
        samples = np.asarray(pit_loss_samples_s, dtype=float)
        pit_losses_s = rng.choice(samples, size=sims) - float(np.median(samples)) + pit_loss_s
    else:
        pit_losses_s = rng.normal(pit_loss_s, _f(th, "plan_mc_pit_loss_sd_s", 1.5), size=sims)
    pit_losses_s = np.maximum(pit_losses_s, np.finfo(float).tiny)

    candidate_count = last_k + 1
    sc_draws = rng.random((sims, candidate_count)) < _f(th, "plan_mc_sc_prob_per_lap", 0.03)
    has_sc = sc_draws.any(axis=1)
    first_sc = np.argmax(sc_draws, axis=1)
    ks = np.arange(candidate_count)[None, :]
    sc_mask = has_sc[:, None] & (ks >= first_sc[:, None]) & (ks <= first_sc[:, None] + 2)
    deltas = _candidate_deltas_s(
        fit=fit,
        fresh_fit=fresh_fit,
        laps_remaining=laps_remaining,
        tyre_age=tyre_age,
        cur_cliff_age=cur_cliff_age,
        deg_samples=deg_samples,
        pit_losses_s=pit_losses_s,
        sc_mask=sc_mask,
        sc_pit_loss_s=sc_pit_loss_s,
        last_k=last_k,
        th=th,
    )
    q = np.quantile(deltas, _f(th, "plan_mc_risk_quantile", 0.8), axis=0)
    k = int(np.argmin(q))
    q_modal_lap = lap_num + k if q[k] < 0 else 0

    best_ks = np.argmin(deltas, axis=1)
    best_deltas = deltas[np.arange(sims), best_ks]
    chosen_laps = np.where(best_deltas < 0.0, lap_num + best_ks, 0)
    lap_values, counts = np.unique(chosen_laps, return_counts=True)
    modal_lap = int(lap_values[counts == counts.max()][0])

    if plan_name in ("cover", "defend") and math.isfinite(cover_gap_s):
        uc = int(_f(th, "undercut_laps", 2))
        ages = np.arange(uc, dtype=float)
        worn_ms = _sampled_current_laps_ms(
            fit=fit,
            tyre_age=tyre_age,
            ages=ages,
            cur_cliff_age=cur_cliff_age,
            deg_samples=deg_samples,
            th=th,
        )
        fresh_laps_ms = fresh_fit.base_ms + fresh_fit.deg_ms_per_lap * ages
        their_gain = np.sum(worn_ms - fresh_laps_ms[None, :], axis=1) / 1000.0
        cover_samples = their_gain - _f(th, "outlap_warmup_s", 0.8) - cover_gap_s
        if plan_name == "cover":
            agree = float(np.mean(cover_samples + _f(th, "cover_margin_s", 0.5) > 0))
        else:
            agree = float(np.mean(-cover_samples >= _f(th, "cover_defend_gain_s", 1.0)))
    else:
        gain_min = _f(mode, "pit_gain_min_s", 1.0)
        min_deltas = deltas.min(axis=1)
        plan_k = plan_lap - lap_num
        if plan_lap == 0:
            agree = float(np.mean(min_deltas >= -gain_min))
        elif 0 <= plan_k <= last_k:
            agree = float(np.mean(deltas[:, plan_k] - min_deltas <= gain_min))
        else:
            agree = 1.0
    return McResult(modal_lap, agree, sims, q_modal_lap)


def scale_plan(plan: PitPlan, mc: McResult | None) -> PitPlan:
    if mc is None or plan.plan in ("", "cheap_stop"):
        return plan
    return replace(
        plan,
        confidence=round(plan.confidence * mc.agree, 3),
        mc_lap=mc.modal_lap,
        mc_agree=round(mc.agree, 3),
        mc_risk_lap=mc.q_modal_lap,
    )
