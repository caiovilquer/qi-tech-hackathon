"""Delivery acceptance through an unmodified app process and real PostgreSQL."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError


ROOT = Path(__file__).resolve().parents[2]
COMMAND = [sys.executable, '-m', 'scripts.demo_pix']
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def environment(**changes):
    env = dict(os.environ, **changes)
    env.pop('PIX_POLICY_FILE', None)
    return env


def start(command, env):
    # Own session: a timeout or a failure must reach the Uvicorn child, not only the runner.
    return subprocess.Popen(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, start_new_session=True)


def stop_group(process):
    for signum, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            return
        try:
            process.communicate(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def group_is_gone(process):
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return True
    return False


def demo(env, timeout=60):
    process = start(COMMAND, env)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stop_group(process)
        raise
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


@pytest.fixture
def server():
    engine = create_engine(os.environ['DATABASE_URL'])
    yield engine
    engine.dispose()


def demo_databases(server):
    with server.connect() as connection:
        return set(connection.execute(text(
            "SELECT datname FROM pg_database WHERE datname LIKE 'pix_test_demo_%'")).scalars())


def drop(server, names):
    admin = create_engine(server.url.set(database='postgres'), isolation_level='AUTOCOMMIT')
    try:
        with admin.connect() as connection:
            for name in names:
                connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        admin.dispose()


def served_requests(server, created):
    """True once the child API has committed an analysis inside a demo database."""
    for name in created:
        engine = create_engine(server.url.set(database=name))
        try:
            with engine.connect() as connection:
                if connection.execute(text('SELECT count(*) FROM risk_analysis')).scalar_one() > 0:
                    return True
        except (OperationalError, ProgrammingError):
            pass  # not migrated yet, or already dropped
        finally:
            engine.dispose()
    return False


def test_delivery_scenarios_survive_process_restart_and_leave_no_database(server):
    before = demo_databases(server)
    result = demo(environment())
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report['first_pix']['status'] == 'approved'
    assert report['device_change']['registered'] == 'approved'
    assert report['device_change']['unknown'] == 'two_factor_required'
    assert report['burst']['statuses'] == ['approved'] * 4 + ['two_factor_required'] * 5 + ['rejected']
    assert report['night']['statuses'] == ['approved', 'two_factor_required', 'two_factor_required', 'rejected']
    assert report['adverse']['status'] == 'rejected'
    assert report['adverse']['reasons'] == ['PAYEE_DICT_ADVERSE', 'PAYEE_DICT_RESTRICTED', 'DICT_CHECK_UNAVAILABLE']
    # The follow-up Pix is older than the maturity window by transaction time, so the
    # only thing keeping the payment immature is that its execution became known now.
    assert report['two_fa_execution'] == {'auth_events': 1, 'execution_events': 1,
        'payments_before': 0, 'payments_after_auth': 0, 'payments_after_execution': 1,
        'mature_after_execution': 0, 'payment_older_than_maturity_window': True,
        'follow_up_status': 'two_factor_required',
        'device_after': 'unregistered', 'original_unchanged': True}
    assert report['replay_after_restart'] == {'http_status': 200, 'identical': True}
    assert report['counts'] == {'analyses': 21, 'pix': 21, 'initial_checks': 6}
    assert report['migrations'] == ['0001_base.sql', '0002_pix_storage.sql']
    assert demo_databases(server) == before


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


def test_scenario_failure_with_live_api_stops_uvicorn_and_drops_database(server):
    before = demo_databases(server)
    script = ("from scripts import demo_pix\n"
              "calls = []\n"
              "real = demo_pix.post\n"
              "def failing(url, body, expected=201):\n"
              "    calls.append(body['id'])\n"
              "    if len(calls) == 6:\n"
              "        raise RuntimeError('forced failure with live API after ' + ','.join(calls))\n"
              "    return real(url, body, expected)\n"
              "demo_pix.post = failing\n"
              "demo_pix.main()\n")
    process = start([sys.executable, '-c', script], environment())
    try:
        stdout, stderr = process.communicate(timeout=60)
        assert process.returncode != 0
        assert 'forced failure with live API after first,old-device,new-device,unknown-device,burst-0,burst-1' in stderr
        assert demo_databases(server) == before
        assert group_is_gone(process), 'Uvicorn child outlived the failed runner'
    finally:
        stop_group(process)
        drop(server, demo_databases(server) - before)


def test_sigterm_during_live_api_stops_uvicorn_and_drops_database(server):
    before = demo_databases(server)
    process = start(COMMAND, environment())
    try:
        deadline = time.monotonic() + 30
        while not served_requests(server, demo_databases(server) - before):
            assert process.poll() is None and time.monotonic() < deadline, 'Demo did not reach the HTTP scenarios'
            time.sleep(0.05)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode == 128 + signal.SIGTERM, stderr  # handled, not the default kill
        assert demo_databases(server) == before
        assert group_is_gone(process), 'Uvicorn child outlived the terminated runner'
    finally:
        stop_group(process)
        drop(server, demo_databases(server) - before)


def test_api_startup_failure_reports_child_output(server, tmp_path):
    from scripts.demo_pix import api_process
    fixture = tmp_path / 'bad.json'
    fixture.write_text('{"unexpected": []}')
    with pytest.raises(RuntimeError, match=r'(?s)Demo API failed to start.*Unknown fixture dataset fields'):
        with api_process(server, fixture):
            pass
