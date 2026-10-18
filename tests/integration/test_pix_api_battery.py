"""Regression evidence for the black-box test battery (D1 to D3): bursts, 404 logs and receipts."""
from concurrent.futures import ThreadPoolExecutor
import logging
from os import environ, pathsep
from pathlib import Path
from socket import socket
from subprocess import Popen, TimeoutExpired
from sys import executable
from tempfile import TemporaryFile
from time import monotonic, sleep

import pytest
import requests
from sqlalchemy import text

from tests.integration.test_pix_api import api, controller, payload, post
from tests.integration.test_pix_storage import storage
from tests.integration.test_migrations import migration_db

ROOT = Path(__file__).resolve().parents[2]


WORKERS = 4  # The Dockerfile default (API_WORKERS)


@pytest.fixture
def served(storage, migration_db):
    """The packaged app in its own uvicorn processes, as in the image, over the per-test database.

    A burst is CPU-bound: an in-process server would share the GIL with the client threads.
    """
    with socket() as listener, TemporaryFile() as logs:
        listener.bind(('127.0.0.1', 0))
        listener.listen(128)
        url = 'http://127.0.0.1:' + str(listener.getsockname()[1])
        env = dict(environ, DATABASE_URL=migration_db.url.render_as_string(hide_password=False),
                   INTERNAL_TOKEN='test_token', PIX_TENANTS='', PIX_FIXTURE_FILE='',
                   PYTHONPATH=str(ROOT / 'src') + pathsep + environ.get('PYTHONPATH', ''))
        env.pop('PIX_POLICY_FILE', None)
        server = Popen([executable, '-m', 'uvicorn', 'app:app', '--fd', str(listener.fileno()),
                        '--workers', str(WORKERS), '--no-access-log'],
                       env=env, cwd=ROOT, pass_fds=(listener.fileno(),), stdout=logs, stderr=logs)
        try:
            until = monotonic() + 20
            while True:
                assert server.poll() is None and monotonic() < until, 'API workers failed to start'
                try:
                    if requests.get(url + '/health_check', timeout=.2).status_code == 204:
                        break
                except (requests.ConnectionError, requests.Timeout):
                    sleep(.05)
            # Every worker imports and connects lazily: warm them before measuring a burst.
            for index in range(2 * WORKERS):
                assert post(url, distinct_payer(1000 + index)).status_code == 201
            yield url
        finally:
            server.terminate()
            try:
                server.wait(10)
            except TimeoutExpired:
                server.kill()
                server.wait(5)


def distinct_payer(index):
    # AccountKey includes the account number: each body is a new payer account.
    body = payload()
    body['source_account']['account_number'] = str(50000 + index)
    return body


def burst(url, bodies):
    with ThreadPoolExecutor(max_workers=len(bodies)) as pool:
        started = monotonic()
        responses = list(pool.map(lambda body: post(url, body), bodies))
    return responses, monotonic() - started


def test_thirty_distinct_simultaneous_posts_are_analysed_without_503(served, storage):
    # Same destination for all: the payers also serialize on its fan-in lock.
    responses, elapsed = burst(served, [distinct_payer(index) for index in range(30)])
    assert [r.status_code for r in responses] == [201] * 30, [r.text for r in responses if r.status_code != 201]
    assert elapsed < 3.5
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM risk_analysis')).scalar() == 30 + 2 * WORKERS
        assert repo.session.execute(text(
            "SELECT count(*) FROM request_receipt WHERE http_status=201 AND category='analysis'")).scalar() == 30 + 2 * WORKERS


def test_twenty_identical_simultaneous_posts_are_one_analysis_and_replays(served, storage):
    body = payload()
    responses, _ = burst(served, [body] * 20)
    assert sorted(r.status_code for r in responses) == [200] * 19 + [201]
    assert len({r.json()['key'] for r in responses}) == 1
    with storage.read() as repo:
        assert repo.session.execute(text('SELECT count(*) FROM pix_transaction WHERE external_id=:id'),
                                    {'id': body['id']}).scalar() == 1
        # D3: every request has a complete receipt.
        assert repo.session.execute(text(
            'SELECT count(*) FROM request_receipt WHERE http_status IS NULL OR category IS NULL')).scalar() == 0


@pytest.mark.parametrize('raw_id', ['z' * 129, 'abc%0Adef', 'abc%E2%80%A8def', 'abc%0D%0Aevil', 'abc%C2%85def'])
def test_get_with_id_outside_the_format_logs_not_found_and_the_raw_path(api, storage, caplog, raw_id):
    url = api(controller(storage))
    with caplog.at_level(logging.INFO):
        response = requests.get(url + '/pix/transaction/' + raw_id, headers={'INTERNAL-TOKEN': 'test_token'}, timeout=6)
    assert response.status_code == 404 and response.json()['code'] == 'QIT002001'
    messages = [record.getMessage() for record in caplog.records]
    assert any(m.startswith('pix.query.not_found ') for m in messages)
    started = next(m for m in messages if m.startswith('request.started '))
    # The logged path is what the client sent: %0A is neither dropped nor emitted raw.
    assert f'path=/pix/transaction/{raw_id}' in started
    assert all('\n' not in m and '\r' not in m for m in messages)
