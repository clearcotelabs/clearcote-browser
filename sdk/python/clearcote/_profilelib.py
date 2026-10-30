"""Profile library: index, host-coherence scoring, and selection.

Port of the Node SDK's ``profilelib.ts``. The two must stay behaviourally identical — a
persona chosen on one SDK should be the persona chosen on another for the same host and key,
or a customer switching languages silently changes identity.

The persona path this serves is ``--fingerprint-profile`` **without** ``--fingerprint``. That
distinction is load-bearing: measured against a strict anti-bot device check, a seed persona failed
13/13 while a profile-only persona passed. With no seed the farbling machinery never engages, so
canvas/WebGL readbacks come back byte-identical to an unmodified browser — the profile is
static value substitution over a real capture rather than perturbation of this machine's.

SELECTION IS ABOUT COHERENCE WITH *THIS HOST*, NOT ABOUT MATCHING AN OS STRING. A profile
claiming an RTX 3080 on an Intel UHD host says NVIDIA in the strings and paints Intel pixels — a
contradiction readable in one call. So the GPU term dominates, and ``browser_major`` is a HARD
filter: a Chrome-138 capture on a 150 engine reports UA 138 while the engine behaves like 150.

ROTATION IS PER-IDENTITY, NOT PER-LAUNCH. Always serving the single best profile makes every
customer on similar hardware converge on one identity, and that shared fingerprint becomes its
own signal. But re-rolling every launch is worse: an account whose device changes on every visit
is a harder tell than a slightly-suboptimal stable one. So the default ("rotate") is sticky per
key — the same key always resolves to the same profile, different keys spread across the top-N.
"""

from __future__ import annotations

import hashlib
import os
import random
import sys
from datetime import datetime, timezone
from typing import Any, Literal, Optional, Sequence

SelectMode = Literal["best", "rotate", "random"]

# Windows caps a process command line at 32767 chars. The profile rides on argv as
# --fingerprint-profile=<gzip+base64>, and the other switches need room too, so keep a wide
# margin. Exceeding it is not a soft failure: Chromium refuses to spawn and the driver surfaces
# an opaque error, which is why this is enforced at SELECTION time rather than at launch.
DEFAULT_MAX_ENCODED = 24000

DEFAULT_TOP_N = 25


def gpu_vendor_class(renderer: Optional[str]) -> str:
    """Coarse GPU vendor class from a renderer string (ANGLE or raw GL)."""
    r = (renderer or "").lower()
    if not r:
        return "unknown"
    if "nvidia" in r or "geforce" in r or "quadro" in r:
        return "nvidia"
    if "amd" in r or "radeon" in r or "ati " in r:
        return "amd"
    if "intel" in r:
        return "intel"
    if "apple" in r:
        return "apple"
    if "swiftshader" in r or "llvmpipe" in r or "software" in r:
        return "software"
    if "mali" in r or "adreno" in r or "powervr" in r:
        return "mobile"
    return "other"


def _closeness(a: Optional[float], b: Optional[float]) -> float:
    """Ratio closeness in [0,1] — 1 when equal, decaying as the values diverge."""
    if a is None or b is None or a <= 0 or b <= 0:
        return 0.5  # unknown: neutral, never a bonus or a penalty
    hi, lo = max(a, b), min(a, b)
    return lo / hi


def score_profile(entry: dict[str, Any], host: dict[str, Any]) -> float:
    """Score a candidate against the host. Higher is better; the maximum is 1.

    Weights encode what a detector can actually catch, in descending order of how cheaply:
      * GPU class (0.45) — a string-vs-render mismatch is a single-call contradiction.
      * Screen (0.25) — a persona whose screen cannot contain the real window is the documented
        block trigger. Claiming a LARGER display is the dangerous direction, so it is penalised
        harder than claiming a smaller one.
      * DPR (0.10), cores+memory (0.10), freshness (0.10).
    """
    score = 0.0

    hv = host.get("gpu_vendor") or "unknown"
    ev = entry.get("gpu_vendor") or "unknown"
    if ev == "software" or hv == "software":
        gpu = 0.0  # a software rasterizer can never paint what a discrete GPU claim implies
    elif ev == hv:
        gpu = 1.0
    elif hv == "unknown" or ev == "unknown":
        gpu = 0.5
    else:
        gpu = 0.15  # different real vendor: strings and pixels disagree
    et, ht = entry.get("gpu_tier"), host.get("gpu_tier")
    if gpu == 1.0 and et is not None and ht is not None:
        gpu = 1.0 - min(1.0, abs(et - ht) / 3.0) * 0.4
    score += 0.45 * gpu

    ew, eh = entry.get("screen_width"), entry.get("screen_height")
    hw, hh = host.get("screen_width"), host.get("screen_height")
    screen = 0.5
    if ew and eh and hw and hh:
        fits = ew <= hw and eh <= hh
        area = _closeness(ew * eh, hw * hh)
        screen = 0.7 + 0.3 * area if fits else 0.25 * area
    score += 0.25 * screen

    score += 0.10 * _closeness(entry.get("device_pixel_ratio"), host.get("device_pixel_ratio"))
    score += 0.05 * _closeness(entry.get("hardware_concurrency"), host.get("hardware_concurrency"))
    score += 0.05 * _closeness(entry.get("device_memory"), host.get("device_memory"))

    fresh = 0.5
    captured = entry.get("captured_at")
    if captured:
        try:
            dt = datetime.fromisoformat(str(captured).replace("Z", "+00:00"))
            days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0
            fresh = max(0.0, min(1.0, 1.0 - (days - 180) / 550))
        except (ValueError, TypeError):
            fresh = 0.5
    score += 0.10 * fresh

    return score


def eligible(
    entry: dict[str, Any],
    host: dict[str, Any],
    os_override: Optional[str] = None,
    max_encoded: int = DEFAULT_MAX_ENCODED,
) -> bool:
    """Hard filters. A candidate failing any of these is incoherent, not merely low-scoring —
    no weight should be able to promote it back into the pool."""
    want = (os_override or host.get("os_family") or "").lower()
    if str(entry.get("os_family", "")).lower() != want:
        return False
    # A version-agnostic profile (browser_major None) carries hardware identity only and inherits
    # the engine's version, so it cannot contradict it — those pass. That is the converted
    # chrome-fingerprints case: its records are Chrome ~114/115 and convert_dataset.py drops the
    # version deliberately. A profile that PINS a version and gets it wrong IS a contradiction —
    # the UA would claim 138 while the engine's observable behaviour is 150 — so it is rejected
    # however well it scores elsewhere.
    entry_major = entry.get("browser_major")
    if entry_major is not None and entry_major != host.get("browser_major"):
        return False
    size = entry.get("encoded_size")
    if size is not None and size > max_encoded:
        return False
    return True


def _key_hash(key: str) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest()[:8], 16)


_cached_sticky_key: Optional[str] = None


def default_sticky_key() -> str:
    """A stable per-machine key, so keyless "rotate" is consistent rather than random."""
    global _cached_sticky_key
    if _cached_sticky_key:
        return _cached_sticky_key
    bits = [
        os.environ.get("CLEARCOTE_PROFILE_KEY"),
        os.environ.get("USERNAME") or os.environ.get("USER"),
        sys.platform,
    ]
    joined = "|".join(b for b in bits if b)
    _cached_sticky_key = hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
    return _cached_sticky_key


def select_profile(
    index: Sequence[dict[str, Any]],
    host: dict[str, Any],
    mode: SelectMode = "rotate",
    key: Optional[str] = None,
    top_n: int = DEFAULT_TOP_N,
    os_override: Optional[str] = None,
    max_encoded: int = DEFAULT_MAX_ENCODED,
) -> dict[str, Any]:
    """Pick a profile for this host.

    Raises when nothing is eligible rather than silently falling back to an incoherent profile —
    a wrong persona is worse than none, because it turns a clean browser into a contradictory one.

    Returns ``{"entry": ..., "score": ..., "pool_size": ..., "mode": ...}``.
    """
    candidates = [e for e in index if eligible(e, host, os_override, max_encoded)]
    if not candidates:
        raise ValueError(
            f"no profile matches host (os={os_override or host.get('os_family')}, "
            f"chromium major={host.get('browser_major')}). The index has {len(index)} entries; "
            "none passed the os/major/size filters. Sync a newer index, or pass an explicit "
            "fingerprint_profile."
        )

    # Tie-break by id so ordering is deterministic across runs, machines and SDKs.
    scored = sorted(
        ({"entry": e, "score": score_profile(e, host)} for e in candidates),
        key=lambda x: (-x["score"], str(x["entry"].get("id", ""))),
    )
    pool_size = len(scored)
    n = max(1, min(top_n, pool_size))
    pool = scored[:n]

    if mode == "best":
        chosen = pool[0]
    elif mode == "random":
        chosen = random.choice(pool)
    else:
        # "rotate": sticky per key. With no key the caller still gets a STABLE profile rather
        # than a new one each launch — an identity whose device changes every visit is a worse
        # tell than a slightly-suboptimal fixed one. Pass a key to spread across accounts.
        chosen = pool[_key_hash(key or default_sticky_key()) % len(pool)]

    return {
        "entry": chosen["entry"],
        "score": chosen["score"],
        "pool_size": pool_size,
        "mode": mode,
    }
