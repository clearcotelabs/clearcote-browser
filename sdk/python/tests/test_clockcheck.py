"""CR-2VFYCD: a computer clock far off makes the engine refuse every token; the SDK says so before launching."""
import base64
import json

import pytest

from clearcote import ClockSkewError, LicenseError
from clearcote._clockcheck import assert_clock_for, clock_skew_problem, format_duration
from clearcote._persona import apply

T = 1_800_000_000  # server time when the token was minted


def tok(p):
    return base64.urlsafe_b64encode(json.dumps(p).encode()).decode().rstrip("=") + ".sig"


RUN = tok({"lic": "l", "lease": "L1", "plan": "pro", "nbf": T - 900, "iat": T, "exp": T + 180})


def test_quiet_inside_the_window():
    assert clock_skew_problem(RUN, T) is None
    assert clock_skew_problem(RUN, T - 900) is None
    assert clock_skew_problem(RUN, T + 179) is None


def test_names_a_clock_behind_with_at_least_how_far():
    msg, skew = clock_skew_problem(RUN, T - 1200)
    assert skew == -1200
    assert "clock is at least 20 min behind" in msg
    assert "it reads 2027-01-15 07:40:00 UTC; the server issued this licence token at 2027-01-15 08:00:00 UTC" in msg
    assert "Set time automatically" in msg


def test_names_a_clock_ahead_past_exp():
    msg, skew = clock_skew_problem(RUN, T + 600, "persona token")
    assert skew == 600
    assert "after the moment this persona token expires" in msg


def test_skips_unreadable_tokens():
    assert clock_skew_problem("hdr.sig", T) is None
    assert clock_skew_problem(tok({"lic": "l"}), T) is None
    assert_clock_for(None)


def test_format_duration():
    assert format_duration(42) == "42 s"
    assert format_duration(192) == "3 min 12 s"
    assert format_duration(7500) == "2 h 5 min"
    assert format_duration(100800) == "1 day 4 h"


class _Lease:
    token = RUN
    lease_id = "L1"


def test_apply_refuses_before_running_the_engine():
    ran = []
    with pytest.raises(ClockSkewError) as e:
        apply("chrome", ["--fingerprint=1"], {}, _Lease(), supports=lambda *_: True,
              runner=lambda *a, **k: ran.append(1) or '{"gpu_vendor":"none"}', fetch=lambda *a, **k: None, now=T - 3600)
    assert isinstance(e.value, LicenseError)
    assert e.value.code == "CLOCK_SKEW"
    assert "at least 1 h behind" in str(e.value)
    assert ran == []
