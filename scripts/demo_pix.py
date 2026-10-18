"""Synthetic delivery demo: real app subprocesses, HTTP and disposable PostgreSQL.

Never resets the supplied database. Only the randomly named database created here
is migrated/dropped. Bank events use the existing trusted internal service.
SIGTERM (docker stop, Compose shutdown) becomes SystemExit, so the API child is
stopped and the database dropped; SIGKILL still requires `down -v` of the stack.
"""
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic, sleep
from uuid import uuid4

import requests
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
TOKEN = 'synthetic-demo-token'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


@contextmanager
def disposable_database():
    url = make_url(os.environ.get('DATABASE_URL', 'postgresql://localhost/invalid'))
    require(os.environ.get('APP_ENV') == 'testing' and url.drivername in
            ('postgresql', 'postgresql+psycopg2') and (url.database or '').startswith('pix_test_'),
            'Refusing demo: requires APP_ENV=testing and a pix_test_ PostgreSQL server target')
    name = 'pix_test_demo_' + uuid4().hex
    admin = create_engine(url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    engine = create_engine(url.set(database=name), pool_pre_ping=True)
    created = False
    try:
        with admin.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{name}"')
        created = True
        yield engine
    finally:
        engine.dispose()
        try:
            if created:
                with admin.connect() as connection:
                    connection.exec_driver_sql(f'DROP DATABASE "{name}" WITH (FORCE)')
        finally:
            admin.dispose()


def tail(logs, size=4000):
    # pread never moves the file offset shared with the child's stdout/stderr.
    length = os.fstat(logs.fileno()).st_size
    return os.pread(logs.fileno(), size, max(0, length - size)).decode(errors='replace').strip()


@contextmanager
def api_process(engine, fixture):
    # Inherited bound socket eliminates the choose-port/launch race. No host port
    # is published by Compose; this listener exists only inside the demo container.
    with socket.socket() as listener, TemporaryFile() as logs:
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        url = 'http://127.0.0.1:' + str(listener.getsockname()[1])
        env = dict(os.environ, DATABASE_URL=engine.url.render_as_string(hide_password=False),
                   INTERNAL_TOKEN=TOKEN, PIX_TENANTS='',
                   PIX_FIXTURE_FILE=str(fixture), PYTHONPATH=str(ROOT / 'src') + os.pathsep +
                   os.environ.get('PYTHONPATH', ''))
        # Ignore an operator's policy override: this demo verifies the packaged V3.
        env.pop('PIX_POLICY_FILE', None)
        worker = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app:app',
            '--fd', str(listener.fileno()), '--no-server-header', '--log-level', 'error'],
            env=env, cwd=ROOT, pass_fds=(listener.fileno(),), stdout=logs, stderr=logs)
        try:
            until = monotonic() + 10
            while True:
                if worker.poll() is not None or monotonic() >= until:
                    raise RuntimeError('Demo API failed to start:\n' + tail(logs))
                try:
                    if requests.get(url + '/health_check', timeout=.2).status_code == 204:
                        break
                except (requests.ConnectionError, requests.Timeout):
                    sleep(.02)
            yield url
        finally:
            if worker.poll() is None:
                worker.terminate()
            try:
                worker.wait(5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(3)


def payload(external, account, at, amount=5000, device='registered-demo', adverse=False):
    return {'id': external, 'amount': amount, 'transaction_date': at.isoformat(),
        'source_account': {'branch': '0001', 'account_number': str(account),
            'opening_date': '2020-01-01T00:00:00Z',
            'owner': {'document_number': '12345678909'}},
        'destination_account': {'participant': '00000000', 'branch': '0002',
            'account_number': '999' if adverse else str(account + 1000),
            'opening_date': '2020-01-01T00:00:00Z',
            'owner': {'document_number': '52998224725' if adverse else '98765432100'}},
        'source': {'device_id': device, 'channel': 'app', 'ip': '198.51.100.23'}}


def fixtures(bodies, now):
    from antifraud.adapter import to_pix
    from antifraud.contract import normalize_payload
    from antifraud.context import DictEvidence, Restriction, SecuritySnapshot
    from providers.contracts import BankRecord, ProviderIdentity, ReputationKey, ReputationRecord, SecurityScope
    from utils.pix_storage_codec import pack
    bank, seen = [], set()
    for body in bodies:
        pix = to_pix(normalize_payload(body), 'demo')
        scope = SecurityScope.from_pix(pix)
        if scope in seen or body['source']['device_id'] == 'unknown-demo':
            continue
        seen.add(scope)
        state = 'unregistered' if body['source']['device_id'] == 'unregistered-demo' else 'registered'
        bank.append(pack(BankRecord(scope, ProviderIdentity('fixture-bank', 'v1'), 'available',
            SecuritySnapshot(now - timedelta(minutes=1), now - timedelta(minutes=1),
                             now + timedelta(hours=1), state), 'synthetic-delivery-demo')))
    key = ReputationKey('demo', '52998224725', ProviderIdentity('fixture-dict', 'v1'), 'person_reputation', 'pix')
    restriction = Restriction(key.document, 'payee', now - timedelta(minutes=1),
                              now - timedelta(minutes=1), now + timedelta(hours=1))
    evidence = DictEvidence(key.document, 'available', restriction.observed_at, restriction.available_at,
        restriction.valid_until, fraud_count=1, restrictions=(restriction,), provenance='synthetic-delivery-demo')
    return {'bank': bank, 'history': [], 'reputation': [pack(ReputationRecord(key, evidence))]}


def post(url, body, expected=201):
    response = requests.post(url + '/pix/transaction', json=body,
                             headers={'INTERNAL-TOKEN': TOKEN}, timeout=5)
    require(response.status_code == expected, f"{body['id']}: expected HTTP {expected}, got {response.status_code}: {response.text}")
    dto = response.json()
    require(set(dto) == {'key', 'status', 'metadata'}, 'Invalid public response keys')
    require(dto['status'] in ('approved', 'two_factor_required', 'rejected'), 'Invalid risk status')
    stored = requests.get(url + '/pix/transaction/' + body['id'],
                          headers={'INTERNAL-TOKEN': TOKEN}, timeout=5)
    require(stored.status_code == 200 and stored.json() == dto, 'GET differs from POST')
    return dto


def run(engine, folder):
    from antifraud.adapter import to_pix
    from antifraud.contract import normalize_payload
    from antifraud.features import build_features
    from antifraud.policy import Policy
    from controllers.pix_storage import PixStorage
    from dtos.pix_storage import EventInput
    from migrations.runner import upgrade
    from utils.pix_storage_codec import fingerprint

    applied = upgrade(engine)
    require(upgrade(engine) == [], 'Repeated migrations must be a no-op')
    storage = PixStorage(sessionmaker(bind=engine, autoflush=False))
    with storage.read() as repo:
        now = repo.now()
    # Relative to PostgreSQL time, always in the past. The local night crosses
    # midnight in Sao Paulo. All inputs/amounts/identities are synthetic.
    base = datetime.combine((now - timedelta(days=2)).date(), time(15), timezone.utc)
    first = payload('first', 100, base, device='unknown-demo')
    devices = [payload('old-device', 200, base, device='old-registered-demo'),
               payload('new-device', 200, base + timedelta(minutes=1)),
               payload('unknown-device', 200, base + timedelta(minutes=2), 30000, 'unknown-demo')]
    burst = [payload(f'burst-{i}', 300, base + timedelta(seconds=i)) for i in range(10)]
    night = [payload(f'night-{i}', 400, base.replace(hour=23) + timedelta(hours=offset), 90000)
             for i, offset in enumerate((0, .5, 3.5, 4.5))]
    adverse = payload('adverse', 500, base, adverse=True)
    two_fa = payload('two-fa', 600, base, 30000, 'unregistered-demo')
    # 25h later: older than the 24h maturity window by transaction time, so only the
    # execution becoming known now can keep that payment immature.
    after = payload('after-execution', 600, base + timedelta(hours=25), device='unregistered-demo')
    fixture = folder / 'fixtures.json'
    fixture.write_text(json.dumps(fixtures([first, *devices, *burst, *night, adverse, two_fa, after], now)))
    report = {'migrations': applied}

    with api_process(engine, fixture) as url:
        first_dto = post(url, first)
        require(first_dto['status'] == 'approved', 'First small Pix should be approved')
        require(first_dto['metadata']['enrichment']['payer'] == {'action': 'queried', 'state': 'not_covered'},
                'Initial uncovered DICT must remain explicit uncertainty')
        report['first_pix'] = {'status': first_dto['status'], 'payer_state': 'not_covered'}
        old, new, unknown = [post(url, body) for body in devices]
        require(old['status'] == new['status'] == 'approved' and
                'DEVICE_CHANGE' not in new['metadata']['reasons'], 'Registered device change was penalized')
        require(unknown['status'] == 'two_factor_required' and
                'DEVICE_POLICY_LIMIT' in unknown['metadata']['reasons'], 'Unknown changed device was trusted')
        report['device_change'] = {'registered': new['status'], 'unknown': unknown['status']}
        report['burst'] = {'statuses': [post(url, body)['status'] for body in burst]}
        require(report['burst']['statuses'] == ['approved'] * 4 + ['two_factor_required'] * 5 + ['rejected'],
                'Burst thresholds differ from the versioned policy')
        report['night'] = {'statuses': [post(url, body)['status'] for body in night]}
        require(report['night']['statuses'] == ['approved', 'two_factor_required', 'two_factor_required', 'rejected'],
                'Night accumulated exposure did not cross midnight correctly')
        adverse_dto = post(url, adverse)
        require(adverse_dto['status'] == 'rejected' and 'PAYEE_DICT_RESTRICTED' in adverse_dto['metadata']['reasons'],
                'Known recipient restriction must reject')
        report['adverse'] = {'status': adverse_dto['status'], 'reasons': adverse_dto['metadata']['reasons']}
        original = post(url, two_fa)
        require(original['status'] == 'two_factor_required', 'Expected a real 2FA decision')
        pix = to_pix(normalize_payload(two_fa), 'demo')
        probe = to_pix(normalize_payload(after), 'demo')
        def payments():
            with storage.read() as repo:
                return len(build_features(repo.context(probe, repo.now(), Policy()), Policy()).paid_payee)
        before = payments()
        # This caller is the simulated trusted bank, not an HTTP event endpoint.
        storage.bind_challenge('demo', 'fixture-bank', pix.id, original['key'], 'demo-challenge', fingerprint(pix))
        with storage.read() as repo:
            occurred = repo.now()
        common = dict(tenant='demo', provider='fixture-bank', payer=pix.payer, external_id=pix.id,
                      analysis_key=original['key'], operation_fingerprint=fingerprint(pix), occurred_at=occurred)
        auth = EventInput(event_id='demo-auth', kind='auth', state='passed', challenge_id='demo-challenge', **common)
        require(storage.ingest(auth) == 'created' and storage.ingest(auth) == 'replay', 'Auth event must be idempotent')
        after_auth = payments()
        require(before == after_auth == 0, '2FA success must not count as execution')
        execution = EventInput(event_id='demo-execution', kind='execution', state='executed', **common)
        require(storage.ingest(execution) == 'created', 'Execution event not persisted')
        following = post(url, after)
        with storage.read() as repo:
            audit = repo.analysis_row('demo', after['id'])['snapshot']
            unchanged = repo.get('demo', two_fa['id']).public == original
            kinds = dict(repo.session.execute(text('SELECT kind, count(*) FROM bank_event GROUP BY kind')).all())
        features = audit['features']
        older = pix.transaction_at <= probe.transaction_at - timedelta(hours=Policy().maturity_hours)
        require(older, 'Follow-up must be outside the maturity window by transaction time')
        require(features['paid_payee_payments'] == 1 and features['mature_payments'] == 0 and
                features['device_state'] == 'unregistered' and unchanged, 'Execution/auth/registration were conflated')
        # Unregistered device (+20) and uncovered recipient (+10) under uncertainty: deterministic 2FA.
        require(following['status'] == 'two_factor_required', 'Unexpected follow-up risk status')
        report['two_fa_execution'] = dict(auth_events=kinds['auth'], execution_events=kinds['execution'],
            payments_before=before, payments_after_auth=after_auth, payments_after_execution=features['paid_payee_payments'],
            mature_after_execution=features['mature_payments'], payment_older_than_maturity_window=older,
            follow_up_status=following['status'], device_after=features['device_state'], original_unchanged=unchanged)
    # Restart the actual API interpreter, not merely create another application.
    # Empty fixture data would change fresh decisions; it cannot change a replay.
    fixture.write_text('{}')
    with api_process(engine, fixture) as url:
        require(post(url, two_fa, 200) == original and post(url, first, 200) == first_dto,
                'Persisted analysis changed after restart/events')
        report['replay_after_restart'] = {'http_status': 200, 'identical': True}
    with storage.read() as repo:
        report['counts'] = {name: repo.session.execute(text('SELECT count(*) FROM ' + table)).scalar_one()
                            for name, table in (('analyses', 'risk_analysis'), ('pix', 'pix_transaction'),
                                                ('initial_checks', 'source_initial_check'))}
    require(report['counts'] == {'analyses': 21, 'pix': 21, 'initial_checks': 6}, 'Duplicate or missing persisted analysis')
    return report


def main():
    # docker stop / Compose shutdown deliver SIGTERM: raise SystemExit so the context
    # managers still stop the API child and drop the disposable database.
    signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(128 + signum))
    with disposable_database() as engine, TemporaryDirectory(prefix='pix-demo-') as folder:
        report = run(engine, Path(folder))
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
