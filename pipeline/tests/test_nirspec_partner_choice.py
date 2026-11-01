"""The partner choice's decision rule (``partner_choice.decide``) on synthetic partner light."""
import numpy as np

from campfire_pipeline.nirspec import partner_choice as PC


def _parts(light2, light3, n=40, var=1.0, rows2=None, rows3=None, seed=1):
    """One detector of a nod with partners 2 and 3: light beyond the sky per bin plus noise."""
    rng = np.random.default_rng(seed)
    N2 = np.ones(n, bool) if rows2 is None else rows2
    N3 = np.zeros(n, bool) if rows3 is None else rows3
    return [{2: dict(E=light2 + rng.normal(0, np.sqrt(var), n), V=np.full(n, var), N=N2),
             3: dict(E=light3 + rng.normal(0, np.sqrt(var), n), V=np.full(n, var), N=N3)}]


def test_broad_light_in_one_partner_drops_it():
    drop, gain, light = PC.decide(_parts(np.full(40, 5.0), np.zeros(40)))
    # per bin: both (5/2)^2 + (1 + 1)/4 = 6.75, without 2: 0 + 1, so 5.75 noise units
    assert drop == 2 and 4.5 < gain < 7 and light[2] > 20


def test_noise_alone_keeps_both():
    for seed in range(20):
        drop, gain, _ = PC.decide(_parts(np.zeros(40), np.zeros(40), seed=seed))
        assert drop is None


def test_a_spike_in_one_bin_keeps_both():
    spike = np.zeros(40)
    spike[17] = 200.0
    drop, _, _ = PC.decide(_parts(spike, np.zeros(40)))
    assert drop is None


def test_light_without_another_source_in_the_row_keeps_both():
    """Light in partner 2 where the trace maps put no other source in its row (an
    uncatalogued source is the neighbour scan's job)."""
    drop, _, _ = PC.decide(_parts(np.full(40, 5.0), np.zeros(40), rows2=np.zeros(40, bool), rows3=np.ones(40, bool)))
    assert drop is None


def test_light_the_own_frame_shares_keeps_both():
    """Light in partner 2 that the nod's own frame carries as well (885945 in p1): the
    subtraction removes it, so partner 2 is right to keep."""
    p = _parts(np.full(40, 30.0), np.zeros(40))
    p[0]['own'] = dict(E=np.full(40, 30.0), V=np.ones(40))
    assert PC.decide(p)[0] is None
    p[0]['own'] = dict(E=np.zeros(40), V=np.ones(40))           # only partner 2 has it: a negative image
    assert PC.decide(p)[0] == 2


def test_one_partner_has_nothing_to_choose():
    p = _parts(np.full(40, 5.0), np.zeros(40))
    del p[0][3]
    assert PC.decide(p)[0] is None
