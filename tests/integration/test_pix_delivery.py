"""Delivery acceptance through an unmodified app process and real PostgreSQL."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[2]


def demo(env):
    return subprocess.run([sys.executable, '-m', 'scripts.demo_pix'], cwd=ROOT,
                          env=env, capture_output=True, text=True, timeout=60)


def test_delivery_scenarios_survive_process_restart_and_leave_no_database():
    engine = create_engine(os.environ['DATABASE_URL'])
    def databases():
        with engine.connect() as connection:
            return set(connection.execute(text("SELECT datname FROM pg_database WHERE datname LIKE 'pix_test_demo_%'")).scalars())
    try:
        before = databases()
        environment = dict(os.environ)
        environment.pop('PIX_POLICY_FILE', None)
        result = demo(environment)
        assert result.returncode == 0, result.stdout + result.stderr
        report = json.loads(result.stdout)
        assert report['first_pix']['status'] == 'approved'
        assert report['device_change']['registered'] == 'approved'
        assert report['device_change']['unknown'] == 'two_factor_required'
        assert report['burst']['statuses'] == ['approved'] * 4 + ['two_factor_required'] * 5 + ['rejected']
        assert report['night']['statuses'] == ['approved', 'two_factor_required', 'two_factor_required', 'rejected']
        assert report['adverse']['status'] == 'rejected'
        assert report['two_fa_execution'] == {'auth_events': 1, 'execution_events': 1,
            'payments_before': 0, 'payments_after_auth': 0, 'payments_after_execution': 1,
            'mature_after_execution': 0, 'device_after': 'unregistered', 'original_unchanged': True}
        assert report['replay_after_restart'] == {'http_status': 200, 'identical': True}
        assert report['counts'] == {'analyses': 21, 'pix': 21, 'initial_checks': 6}
        assert report['migrations'] == ['0001_base.sql', '0002_pix_storage.sql']
        assert databases() == before
    finally:
        engine.dispose()


@pytest.mark.parametrize('changes', [{'APP_ENV': 'local'},
    {'DATABASE_URL': 'postgresql+psycopg2://unused:unused@127.0.0.1:1/bootcamp'}])
def test_demo_refuses_application_environment_before_connecting(changes):
    result = demo(dict(os.environ, **changes))
    assert result.returncode != 0
    assert 'Refusing demo' in result.stderr
    assert 'OperationalError' not in result.stderr


def test_demo_removes_only_its_database_when_a_scenario_fails():
    engine = create_engine(os.environ['DATABASE_URL'])
    def databases():
        with engine.connect() as connection:
            return set(connection.execute(text('SELECT datname FROM pg_database')).scalars())
    try:
        before = databases()
        script = "from scripts import demo_pix\ndef failed(*args):\n    raise RuntimeError('forced scenario failure')\ndemo_pix.run = failed\ndemo_pix.main()\n"
        result = subprocess.run([sys.executable, '-c', script], cwd=ROOT, env=dict(os.environ),
                                capture_output=True, text=True, timeout=10)
        assert result.returncode != 0 and 'forced scenario failure' in result.stderr
        assert databases() == before
    finally:
        engine.dispose()
