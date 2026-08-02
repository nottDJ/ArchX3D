"""
ArchX3D — Evidence and Fusion
=============================
The mechanism that turns many partial signals into one decision with a
confidence and an explanation.

The problem with the obvious approaches
---------------------------------------
*Take the highest-priority signal that fired* discards corroboration
entirely: a bedroom with a "BED" label, a bed block, a wardrobe and an
adjacent bathroom scores exactly as confidently as one with only the label.
It also has no way to notice that two signals disagree.

*Average the signals* is worse in the other direction: it lets six weak
geometric heuristics outvote an explicit room label, which is precisely the
failure the project's design philosophy forbids.

What this module does instead
-----------------------------
Every signal emits ``Evidence`` carrying a log-likelihood contribution per
candidate type. Contributions **add**, then a softmax turns the totals into a
posterior. Adding in log space is the right operation for independent
observations, and it gives both desired behaviours for free: corroboration
accumulates, and a single strong contribution (+4.2 for a toilet) genuinely
cannot be overturned by a handful of +0.3s.

The trust hierarchy rides on top as a **veto**, not as a weight. A signal
marked ``authoritative`` — an explicit room label, a ``ROOM_NAME`` attribute,
a user's decision — *states* the answer rather than evidencing it. When one is
present, fusion still runs, but only to decide whether the remaining evidence
corroborates or contradicts the stated answer, which is reported as confidence
rather than allowed to change the outcome. That is "never guess if reliable
information exists" implemented literally.

Conflicts are surfaced, never silently resolved. A room labelled STORE that
contains a toilet is a real drawing error, and the useful behaviour is to
report both readings and let a human decide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass
class Evidence:
    """One signal's contribution to a classification.

    ``scores`` maps candidate label -> log-likelihood contribution. Absent
    labels contribute nothing, which is different from contributing zero only
    in that it keeps the record small.
    """

    #: Short machine-readable signal name, e.g. ``"room_label"``.
    signal: str
    #: Trust tier from ``cad.Source``; lower is more trustworthy.
    tier: int
    #: Label -> log-likelihood contribution.
    scores: Dict[str, float] = field(default_factory=dict)
    #: Human-readable justification, shown to users verbatim.
    reason: str = ""
    #: Which ``cad.Source`` produced it.
    source: str = ""
    #: The entity this evidence came from, for traceability.
    entity_uid: str = ""
    #: When true, this evidence *states* the answer (see module docstring).
    authoritative: bool = False
    #: Multiplier applied to every score. Lets one signal be down-weighted
    #: for a reason specific to the instance (a label far from the centroid,
    #: a low-confidence detection) without changing the table it came from.
    weight: float = 1.0

    def weighted(self) -> Dict[str, float]:
        if self.weight == 1.0:
            return dict(self.scores)
        return {label: value * self.weight for label, value in self.scores.items()}

    @property
    def best_label(self) -> str:
        """The label this evidence most supports; "" when it supports none."""
        positive = {k: v for k, v in self.scores.items() if v > 0}
        return max(positive, key=positive.get) if positive else ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "signal": self.signal,
            "tier": self.tier,
            "source": self.source,
            "scores": {k: round(v, 3) for k, v in sorted(
                self.scores.items(), key=lambda kv: -kv[1]
            )},
            "reason": self.reason,
            "entity_uid": self.entity_uid,
            "authoritative": self.authoritative,
            "weight": round(self.weight, 3),
        }


@dataclass
class Conflict:
    """Two pieces of evidence that point at incompatible answers."""

    claimed: str
    contradicted_by: str
    signal: str
    detail: str
    severity: float = 0.5

    def to_dict(self) -> Dict[str, object]:
        return {
            "claimed": self.claimed,
            "contradicted_by": self.contradicted_by,
            "signal": self.signal,
            "detail": self.detail,
            "severity": round(self.severity, 3),
        }


@dataclass
class FusionResult:
    """The outcome of combining evidence about one subject."""

    label: str
    confidence: float
    #: Posterior over every candidate, highest first.
    posterior: Dict[str, float] = field(default_factory=dict)
    #: Raw summed log-likelihoods, kept for debugging the priors.
    totals: Dict[str, float] = field(default_factory=dict)
    #: Every contributing signal, strongest first.
    evidence: List[Evidence] = field(default_factory=list)
    #: The handful of reasons worth showing a user.
    reasons: List[str] = field(default_factory=list)
    conflicts: List[Conflict] = field(default_factory=list)
    #: Set when an authoritative signal decided the answer.
    decided_by: str = ""
    #: The runner-up, so "92% bedroom, 5% office" can be shown.
    runner_up: str = ""
    runner_up_confidence: float = 0.0

    @property
    def is_confident(self) -> bool:
        return self.confidence >= 0.65 and self.label != "unknown"

    def to_dict(self) -> Dict[str, object]:
        return {
            "label": self.label,
            "confidence": round(self.confidence, 3),
            "runner_up": self.runner_up,
            "runner_up_confidence": round(self.runner_up_confidence, 3),
            "decided_by": self.decided_by,
            "posterior": {k: round(v, 4) for k, v in self.posterior.items()},
            "reasons": list(self.reasons),
            "conflicts": [c.to_dict() for c in self.conflicts],
            "evidence": [e.to_dict() for e in self.evidence],
        }


# ---------------------------------------------------------------------------
# Fusion
# ---------------------------------------------------------------------------

#: Softmax temperature.
#:
#: 1.0, because the accumulated scores already *are* log-likelihoods: dividing
#: them by anything else discards the calibration the priors were written to
#: express. An earlier value of 2.0 was chosen to avoid over-confidence and
#: instead over-damped — a decisive toilet reached only 54% against a field of
#: fourteen candidates, which read as "probably a bathroom" when the evidence
#: said "certainly". Saturation is prevented by ``MAX_CONFIDENCE`` instead,
#: which is the honest place for it: the cap is a statement about the limits
#: of any single observation, not a distortion of the evidence.
TEMPERATURE = 1.0

#: No amount of agreeing evidence expresses certainty. A drawing can be wrong.
MAX_CONFIDENCE = 0.99

#: Confidence assigned when an authoritative signal decides, before
#: corroboration adjusts it.
AUTHORITATIVE_BASE = 0.90

#: How far corroboration or contradiction can move an authoritative answer.
AUTHORITATIVE_BAND = 0.09

#: Below this posterior the answer is reported as ``unknown`` rather than as a
#: low-confidence guess. Guessing is what this system exists to avoid.
MIN_DECIDABLE_CONFIDENCE = 0.30


def fuse(
    evidence: Sequence[Evidence],
    candidates: Sequence[str],
    *,
    unknown_label: str = "unknown",
    max_reasons: int = 6,
) -> FusionResult:
    """Combine evidence into a labelled, explained, calibrated decision."""
    evidence = [e for e in evidence if e.scores]
    if not evidence:
        return FusionResult(
            label=unknown_label, confidence=0.0,
            reasons=["no evidence available"],
        )

    # ---- 1. Sum log-likelihoods -----------------------------------------
    #
    # Two sums, deliberately. ``totals`` is everything, and drives the
    # posterior when nothing authoritative is present. ``independent`` excludes
    # authoritative evidence, and is the *only* basis for judging whether an
    # authoritative claim is corroborated: including a claim in its own
    # corroboration check makes it corroborate itself, and no conflict could
    # ever be detected.
    totals: Dict[str, float] = {label: 0.0 for label in candidates}
    independent: Dict[str, float] = {label: 0.0 for label in candidates}

    for item in evidence:
        weighted = item.weighted()
        for label, value in weighted.items():
            if label not in totals:
                continue
            totals[label] += value
            if not item.authoritative:
                independent[label] += value

    posterior = _softmax(totals, TEMPERATURE)
    ranked = sorted(posterior.items(), key=lambda kv: -kv[1])
    best_label, best_confidence = ranked[0]
    runner_up, runner_up_confidence = (ranked[1] if len(ranked) > 1 else ("", 0.0))

    # ---- 2. Let an authoritative statement override the vote ------------
    authorities = [e for e in evidence if e.authoritative and e.best_label]
    decided_by = ""
    conflicts: List[Conflict] = []

    if authorities:
        # Most trusted first; ties broken by the strength of the claim.
        authorities.sort(key=lambda e: (e.tier, -max(e.scores.values(), default=0.0)))
        primary = authorities[0]
        stated = primary.best_label
        decided_by = primary.signal

        # Two authorities disagreeing is a genuine drawing defect, not
        # something to average away.
        for other in authorities[1:]:
            if other.best_label and other.best_label != stated:
                conflicts.append(Conflict(
                    claimed=stated,
                    contradicted_by=other.best_label,
                    signal=other.signal,
                    detail=(
                        f"{primary.signal} says {stated!r} but {other.signal} "
                        f"says {other.best_label!r}"
                    ),
                    severity=0.9,
                ))

        # Corroboration adjusts confidence within a band around the base. The
        # stated answer does not change; how sure we are about it does.
        agreement = _agreement(independent, stated)
        confidence = AUTHORITATIVE_BASE + AUTHORITATIVE_BAND * agreement
        if conflicts:
            confidence -= 0.15

        # Independent evidence pointing somewhere else is worth reporting even
        # when it does not change the outcome — a room labelled STORE that
        # contains a toilet is a real drawing defect, and the useful behaviour
        # is to state both readings rather than to quietly pick one.
        independent_posterior = _softmax(independent, TEMPERATURE)
        if any(abs(v) > 1e-9 for v in independent.values()):
            challenger, challenger_confidence = max(
                independent_posterior.items(), key=lambda kv: kv[1]
            )
            if challenger != stated and challenger_confidence > 0.5:
                conflicts.append(Conflict(
                    claimed=stated,
                    contradicted_by=challenger,
                    signal="fused_evidence",
                    detail=(
                        f"{primary.signal} states {stated!r}, but the independent "
                        f"evidence favours {challenger!r} "
                        f"({challenger_confidence:.0%})"
                    ),
                    severity=min(0.9, challenger_confidence),
                ))
                confidence -= 0.10

        result_label = stated
        confidence = max(0.0, min(MAX_CONFIDENCE, confidence))
        # Re-rank the posterior so the stated answer reads as the winner.
        posterior = _pin(posterior, stated, confidence)
        ranked = sorted(posterior.items(), key=lambda kv: -kv[1])
        runner_up, runner_up_confidence = (ranked[1] if len(ranked) > 1 else ("", 0.0))
    else:
        result_label = best_label
        # Capped for the same reason as the authoritative path: a pile of
        # agreeing fixtures is strong evidence, not proof.
        confidence = min(best_confidence, MAX_CONFIDENCE)

        # A near-tie is not a decision. Reporting 34% vs 33% as an answer
        # would be exactly the confident guess the brief rules out.
        if confidence < MIN_DECIDABLE_CONFIDENCE:
            result_label = unknown_label
            # An unidentified room has no confidence, rather than a small
            # amount of confidence in nothing. The posterior is still returned
            # in full, so how close the call was stays inspectable.
            confidence = 0.0
        elif runner_up_confidence > 0 and (confidence - runner_up_confidence) < 0.08:
            conflicts.append(Conflict(
                claimed=best_label,
                contradicted_by=runner_up,
                signal="fused_evidence",
                detail=(
                    f"{best_label!r} ({confidence:.0%}) and {runner_up!r} "
                    f"({runner_up_confidence:.0%}) are too close to separate"
                ),
                severity=0.6,
            ))

    # ---- 3. Explain ------------------------------------------------------
    reasons = _explain(evidence, result_label, max_reasons)

    return FusionResult(
        label=result_label,
        confidence=round(confidence, 4),
        posterior={k: round(v, 4) for k, v in ranked},
        totals={k: round(v, 3) for k, v in sorted(totals.items(), key=lambda kv: -kv[1])},
        evidence=sorted(
            evidence, key=lambda e: -abs(e.scores.get(result_label, 0.0) * e.weight)
        ),
        reasons=reasons,
        conflicts=conflicts,
        decided_by=decided_by,
        runner_up=runner_up,
        runner_up_confidence=round(runner_up_confidence, 4),
    )


def _softmax(totals: Dict[str, float], temperature: float) -> Dict[str, float]:
    """Normalise summed log-likelihoods into a posterior.

    Shifted by the maximum before exponentiating, which is numerically
    necessary: a +4.2 contribution repeated across several fixtures reaches
    exp(20) and overflows a naive implementation.
    """
    if not totals:
        return {}
    peak = max(totals.values())
    weights = {
        label: math.exp((value - peak) / max(temperature, 1e-6))
        for label, value in totals.items()
    }
    total = sum(weights.values())
    if total <= 0:
        uniform = 1.0 / len(totals)
        return {label: uniform for label in totals}
    return {label: value / total for label, value in weights.items()}


def _agreement(totals: Dict[str, float], label: str) -> float:
    """How well the non-authoritative evidence supports ``label``, in [-1, 1].

    +1 means the independent evidence would have reached the same answer
    strongly; -1 means it points firmly elsewhere.
    """
    if label not in totals:
        return 0.0
    own = totals[label]
    others = [v for k, v in totals.items() if k != label]
    if not others:
        return 0.0
    best_other = max(others)
    margin = own - best_other
    # ±3 log-units is a decisive margin; scale to ±1 and clamp.
    return max(-1.0, min(1.0, margin / 3.0))


def _pin(posterior: Dict[str, float], label: str, confidence: float) -> Dict[str, float]:
    """Force ``label`` to ``confidence``, rescaling the rest proportionally.

    Keeps the posterior a genuine distribution after an authoritative override,
    so a caller can still read "and the second most likely reading was...".
    """
    remainder = max(0.0, 1.0 - confidence)
    others = {k: v for k, v in posterior.items() if k != label}
    total = sum(others.values())

    pinned = {label: confidence}
    if total > 0:
        for key, value in others.items():
            pinned[key] = value / total * remainder
    elif others:
        share = remainder / len(others)
        for key in others:
            pinned[key] = share
    return pinned


def _explain(
    evidence: Sequence[Evidence], label: str, limit: int
) -> List[str]:
    """The most load-bearing reasons for the chosen label.

    Ranked by how much each signal actually moved *this* answer, so the
    explanation reflects the decision rather than listing everything observed.
    Contradicting evidence is deliberately included — an explanation that
    hides the counter-evidence is a worse explanation.
    """
    scored: List[Tuple[float, str]] = []
    for item in evidence:
        contribution = item.scores.get(label, 0.0) * item.weight
        if abs(contribution) < 0.05 or not item.reason:
            continue
        prefix = "" if contribution > 0 else "against: "
        scored.append((abs(contribution), f"{prefix}{item.reason}"))

    scored.sort(key=lambda pair: -pair[0])

    seen = set()
    reasons = []
    for _, reason in scored:
        if reason in seen:
            continue
        seen.add(reason)
        reasons.append(reason)
        if len(reasons) >= limit:
            break
    return reasons


# ---------------------------------------------------------------------------
# Construction helpers
# ---------------------------------------------------------------------------


def from_table(
    signal: str,
    tier: int,
    table: Dict[str, float],
    reason: str,
    *,
    source: str = "",
    entity_uid: str = "",
    weight: float = 1.0,
    authoritative: bool = False,
) -> Optional[Evidence]:
    """Build ``Evidence`` from a taxonomy lookup, or ``None`` if it is empty.

    Returning ``None`` for an empty table keeps callers from having to check:
    a category nobody has priors for contributes nothing and should not appear
    in the evidence list at all.
    """
    if not table:
        return None
    return Evidence(
        signal=signal, tier=tier, scores=dict(table), reason=reason,
        source=source, entity_uid=entity_uid, weight=weight,
        authoritative=authoritative,
    )


def single(
    signal: str,
    tier: int,
    label: str,
    score: float,
    reason: str,
    **kwargs,
) -> Evidence:
    """``Evidence`` supporting exactly one label."""
    return Evidence(
        signal=signal, tier=tier, scores={label: score}, reason=reason, **kwargs
    )
