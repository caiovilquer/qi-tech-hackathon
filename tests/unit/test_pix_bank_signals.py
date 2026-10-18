"""V4 bank facts: theft/duress rejection floors and the score-only rejection ceilings."""
from dataclasses import replace
from datetime import timedelta

import pytest

from antifraud.context import APPROVED, REJECTED, TWO_FA, SecuritySnapshot
from antifraud.engine import evaluate
from tests.support.pix_permutation_matrix import build_context
from utils.pix_storage_codec import pack, unpack

# PF burst to a two-day-old account, eight times the profile median: score-only rejection.
SCORE_REJECTED = dict(origem="PF", destino="PF", horario="dia", valor="alto", idade_origem="velha",
                      idade_destino="recente", tentativas="multiplas", recorrencia="primeira",
                      localizacao="habitual")
QUIET = dict(SCORE_REJECTED, valor="baixo", idade_destino="velha", tentativas="unica")


def with_security(context, **changes):
    return replace(context, security=replace(context.security, **changes))


def codes(decision):
    return [reason.code for reason in decision.reasons]


def test_fixture_is_a_score_only_rejection():
    decision = evaluate(build_context(5, SCORE_REJECTED))
    assert decision.status == REJECTED and decision.score >= 70
    assert not any(reason.floor == REJECTED for reason in decision.reasons)


@pytest.mark.parametrize('flag,code', [('theft_reported', 'DEVICE_THEFT_REPORTED'),
                                       ('duress_reported', 'DURESS_REPORTED')])
def test_reported_theft_or_duress_rejects_even_a_quiet_pix_without_points(flag, code):
    base = build_context(5, QUIET)
    assert evaluate(base).status == APPROVED
    decision = evaluate(with_security(base, **{flag: True}))
    assert decision.status == REJECTED and code in codes(decision)
    assert decision.score == evaluate(base).score


@pytest.mark.parametrize('flag', ['theft_reported', 'duress_reported', 'context_authorized'])
def test_expired_bank_context_carries_no_signal(flag):
    for factors in (QUIET, SCORE_REJECTED):
        base = build_context(5, factors)
        expired = with_security(base, valid_until=base.decision_at - timedelta(seconds=1), **{flag: True})
        stale = with_security(base, valid_until=base.decision_at - timedelta(seconds=1))
        assert evaluate(expired) == evaluate(stale)


def test_authorization_caps_only_a_score_rejection():
    rejected = build_context(5, SCORE_REJECTED)
    capped = evaluate(with_security(rejected, context_authorized=True))
    assert capped.status == TWO_FA and codes(capped)[-1] == 'BANK_AUTHORIZED_CONTEXT'
    assert capped.score == evaluate(rejected).score
    quiet = evaluate(with_security(build_context(5, QUIET), context_authorized=True))
    assert quiet.status == APPROVED and 'BANK_AUTHORIZED_CONTEXT' not in codes(quiet)


def test_authorization_never_lifts_a_rejection_floor():
    authorized = with_security(build_context(5, SCORE_REJECTED), context_authorized=True)
    for changes in ({'device_state': 'revoked'}, {'bank_limit_exceeded': True}, {'theft_reported': True}):
        decision = evaluate(with_security(authorized, **changes))
        assert decision.status == REJECTED and 'BANK_AUTHORIZED_CONTEXT' not in codes(decision)


def test_same_holder_caps_a_score_rejection_but_not_the_night_hard_limit():
    same = build_context(10, dict(SCORE_REJECTED, valor="medio"))
    assert same.pix.same_holder
    decision = evaluate(same)
    assert decision.score >= 70 and decision.status == TWO_FA and 'SAME_HOLDER_TRANSFER' in codes(decision)
    restricted = evaluate(with_security(same, device_state='revoked'))
    assert restricted.status == REJECTED and 'SAME_HOLDER_TRANSFER' not in codes(restricted)


def test_snapshot_stored_before_v4_fields_still_decodes_with_their_defaults():
    snapshot = build_context(5, QUIET).security
    stored = pack(snapshot)
    for name in ('theft_reported', 'duress_reported', 'context_authorized'):
        del stored['value']['data'][name]
    assert unpack(stored) == snapshot
    del stored['value']['data']['observed_at']  # No default: still mandatory.
    with pytest.raises(ValueError):
        unpack(stored)
    assert isinstance(snapshot, SecuritySnapshot)
