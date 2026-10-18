"""Differential check against the unmodified V3, outside runtime and pytest.

Run: PYTHONPATH=src .venv/bin/python tests/reference/verify_pix_reference.py
The CI mounts reference documents read-only into the test container.
"""
import argparse
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import timedelta
import importlib.util
import json
from pathlib import Path
from random import Random
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'src'))
from antifraud import context as domain
from antifraud.engine import evaluate
from tests.support.pix_scenarios import BASE, example_pix, cpf_fixture, scenarios


def verify(reference_dir):
    spec = importlib.util.spec_from_file_location('pix_v3_reference', reference_dir / 'politica_v2_simulacao.py')
    reference = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = reference
    spec.loader.exec_module(reference)

    def convert(value):
        if is_dataclass(value):
            return getattr(reference, type(value).__name__)(**{
                field.name: convert(getattr(value, field.name)) for field in fields(value)})
        if isinstance(value, tuple):
            return tuple(convert(item) for item in value)
        return value

    expected = json.loads((reference_dir / 'politica_simulacao_resultados.json').read_text())
    actual = json.loads(json.dumps(scenarios()))
    assert actual == expected['scenarios'], 'Scenario observations diverged'
    print(f'{len(actual)} archived scenario observations match the production engine')

    rng = Random(20261018)
    for case in range(2000):
        current = example_pix('current', rng.choice([1, 20000, 100000, 100001, 300001, 500001, 100000000000]),
                              BASE.replace(hour=rng.choice([0, 6, 14, 20, 23])),
                              opened=BASE-timedelta(days=rng.choice([1, 7, 30, 300])))
        history, executions, authentications = [], [], []
        for index in range(rng.randrange(21)):
            prior = replace(current, id=f'prior-{index}', amount_cents=rng.randrange(1, 600001),
                            transaction_at=current.transaction_at-timedelta(minutes=rng.choice([0, 1, 9, 10, 11, 1440, 2880, 100000, 150000])),
                            device_id=rng.choice(['dev-1', 'old-device']))
            if rng.randrange(4) == 0:
                prior = replace(prior, payer=replace(prior.payer, document=cpf_fixture(index+1), account=str(index+1)))
            if rng.randrange(8) == 0:
                prior = replace(prior, payer=replace(prior.payer, tenant='other'))
            status = rng.choice([domain.APPROVED, domain.TWO_FA, domain.REJECTED])
            history.append(domain.HistoricalAnalysis(prior, prior.transaction_at, index+1, status,
                'challenge', rng.choice(['registered', 'unregistered', 'unknown']), rng.choice([True, False])))
            if rng.randrange(2):
                executions.append(domain.ExecutionEvent(f'exec-{index}', prior.payer, prior.id,
                    rng.choice(['executed', 'failed', 'cancelled']), prior.transaction_at,
                    rng.choice([BASE-timedelta(days=1), BASE+timedelta(days=1)])))
            if rng.randrange(2):
                authentications.append(domain.AuthEvent(f'auth-{index}', prior.payer, prior.id,
                    'challenge', rng.choice(['passed', 'failed']), True, prior.transaction_at, prior.transaction_at))
        security = domain.SecuritySnapshot(BASE-timedelta(days=1), BASE-timedelta(days=1),
            BASE+timedelta(days=rng.choice([-1, 1])), rng.choice(['registered', 'unregistered', 'revoked', 'unknown']),
            trusted_beneficiary=rng.choice([True, False]), bank_limit_exceeded=rng.choice([True, False]))
        evidence = tuple((role, domain.DictEvidence(document, rng.choice(['available', 'not_covered', 'unavailable', 'stale']),
            BASE-timedelta(days=1), BASE-timedelta(days=1), BASE+timedelta(days=rng.choice([-1, 1])),
            fraud_count=rng.randrange(2), open_reports=rng.randrange(2))) for role, document in
            [('payer', current.payer.document), ('payee', current.payee_document)])
        context = domain.Context(current, current.transaction_at, tuple(history), tuple(executions),
            tuple(authentications), security, evidence, ('payer', 'payee'))
        expected_decision, actual_decision = asdict(reference.evaluate(convert(context))), asdict(evaluate(context))
        assert actual_decision == expected_decision, f'Differential mismatch at seed 20261018 case {case}: {actual_decision} != {expected_decision}'
    print('2000 seeded contexts match the complete V3 Decision (not only status)')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference-dir', type=Path, default=ROOT / 'docs' / 'contexto')
    verify(parser.parse_args().reference_dir)
