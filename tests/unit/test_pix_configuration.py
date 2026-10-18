"""Configuration failure boundaries and tenant/version isolation."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from antifraud.configuration import DEFAULT_POLICY_PATH, PolicyCatalog, load_policy_catalog
from antifraud.policy import Policy, PolicyConfigurationError
from antifraud.rules import CRITERIA

ROOT = Path(__file__).resolve().parents[2]


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.raw = json.loads(DEFAULT_POLICY_PATH.read_text())
        self.config = self.raw['policies']['pix-experimental-v3']

    def rejected(self):
        with self.assertRaises(PolicyConfigurationError):
            PolicyCatalog.from_mapping(self.raw)

    def test_shipped_configuration_equals_reference_defaults(self):
        policy = load_policy_catalog().for_tenant('demo')
        self.assertEqual(policy, Policy())
        self.assertEqual(len(CRITERIA), 25)
        self.assertEqual(set(self.config['criteria']), set(CRITERIA))

    def test_unknown_tenant_never_inherits_policy(self):
        with self.assertRaises(KeyError):
            load_policy_catalog().for_tenant('other')

    def test_isolated_versions_and_immutable_loaded_copy(self):
        alternative = deepcopy(self.config)
        alternative['criteria']['LOW_HISTORY_EXPOSURE']['low_points'] = 5
        self.raw['policies']['pix-experimental-v4'] = alternative
        self.raw['tenants']['other'] = 'pix-experimental-v4'
        catalog = PolicyCatalog.from_mapping(self.raw)
        first, other = catalog.for_tenant('demo'), catalog.for_tenant('other')
        self.assertNotEqual(first.fingerprint, other.fingerprint)
        self.raw['policies'].clear()
        alternative['criteria'].clear()
        self.assertEqual(dict(first.parameters_for('LOW_HISTORY_EXPOSURE'))['low_points'], 15)
        self.assertEqual(dict(other.parameters_for('LOW_HISTORY_EXPOSURE'))['low_points'], 5)
        with self.assertRaises(FrozenInstanceError):
            first.version = 'changed'

    def test_fingerprint_independent_of_mapping_order(self):
        first = PolicyCatalog.from_mapping(self.raw).for_tenant('demo')
        self.config['parameters'] = dict(reversed(list(self.config['parameters'].items())))
        self.config['criteria'] = dict(reversed(list(self.config['criteria'].items())))
        second = PolicyCatalog.from_mapping(self.raw).for_tenant('demo')
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)
        self.assertNotEqual(first.fingerprint, replace(first, version='another-version').fingerprint)

    def test_configuration_returns_independent_maps(self):
        policy = Policy()
        snapshot = policy.configuration()
        snapshot['parameters']['family_caps']['Acesso'] = 0
        snapshot['criteria']['UNREGISTERED_DEVICE']['points'] = 0
        self.assertEqual(dict(policy.family_caps)['Acesso'], 40)
        self.assertEqual(dict(policy.parameters_for('UNREGISTERED_DEVICE'))['points'], 20)

    def test_missing_unknown_and_disable_switch_rejected(self):
        for change in ('missing', 'unknown', 'disabled'):
            with self.subTest(change=change):
                self.setUp()
                if change == 'missing':
                    del self.config['criteria']['DEVICE_REVOKED']
                elif change == 'unknown':
                    self.config['criteria']['EXECUTE_CODE'] = {}
                else:
                    self.config['criteria']['DEVICE_REVOKED']['enabled'] = False
                self.rejected()

    def test_invalid_weights(self):
        for value in (-1, True, 1.5, '30', None):
            with self.subTest(value=value):
                self.config['criteria']['DICT_CHECK_UNAVAILABLE']['points'] = value
                self.rejected()

    def test_invalid_parameters(self):
        for name, value in (
            ('history_days', 0), ('history_days', 366), ('maturity_hours', 8761),
            ('velocity_minutes', 525601), ('dict_ttl_seconds', 31536001),
            ('recent_account_days', [7, 366]), ('low_history_cents', [10, 10]),
            ('velocity_pf', [10, 5]), ('velocity_pj', [25]),
            ('structuring_limits', []), ('structuring_limits', [1, 1]),
            ('dict_threshold', 101), ('structuring_percent', -1),
            ('uncertainty_cap', 101), ('night_start_hour', 24),
            ('night_end_hour', 21), ('night_soft_cents', 300000),
            ('two_fa_at', 70), ('daily_cents', True), ('pj_factor', 1.0),
        ):
            with self.subTest(parameter=name, value=value):
                self.setUp()
                self.config['parameters'][name] = value
                self.rejected()

    def test_missing_family_and_extra_parameter_rejected(self):
        del self.config['parameters']['family_caps']['Acesso']
        self.rejected()
        self.setUp()
        self.config['parameters']['arbitrary_python'] = 'os.system()'
        self.rejected()

    def test_catalog_unknown_version_and_bad_tenant(self):
        self.raw['tenants']['demo'] = 'unknown'
        self.rejected()
        for tenant in ('', 'demo\n', 'a' * 65, '../other'):
            with self.subTest(tenant=tenant):
                self.setUp()
                self.raw['tenants'][tenant] = 'pix-experimental-v3'
                self.rejected()

    def test_direct_mutable_policy_values_rejected(self):
        for kwargs in ({'rule_parameters': []}, {'family_caps': list(Policy().family_caps)},
                       {'low_history_cents': [100000, 500000]}):
            with self.subTest(kwargs=kwargs), self.assertRaises(PolicyConfigurationError):
                Policy(**kwargs)
        with self.assertRaises(PolicyConfigurationError):
            PolicyCatalog(list(load_policy_catalog().policies), (('demo', 'pix-experimental-v3'),))

    def test_policy_file_invalid_json_duplicates_nonfinite_and_missing(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / 'policy.json'
            for text in ('{', '{"policies":{},"policies":{},"tenants":{}}',
                         '{"policies":NaN,"tenants":{}}', '{"policies":Infinity}', '[]'):
                with self.subTest(text=text), self.assertRaises(PolicyConfigurationError):
                    target.write_text(text)
                    load_policy_catalog(target)
            with self.assertRaises(PolicyConfigurationError):
                load_policy_catalog(Path(folder) / 'missing.json')

    def test_invalid_startup_configuration_aborts_app_import(self):
        with TemporaryDirectory() as folder:
            target = Path(folder) / 'invalid.json'
            target.write_text('{}')
            result = self.import_app(target)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('PolicyConfigurationError', result.stderr)

    def test_valid_startup_freezes_catalog_and_preserves_routes(self):
        result = self.import_app(DEFAULT_POLICY_PATH, """
from app import app
assert app.state.pix_policies.for_tenant('demo').version == 'pix-experimental-v3'
assert {'/pix/transaction', '/pix/transaction/{id}'} <= {route.path for route in app.routes}
""")
        self.assertEqual(result.returncode, 0, result.stderr)

    def import_app(self, path, code='import app'):
        env = {**os.environ, 'PYTHONPATH': str(ROOT / 'src'), 'APP_ENV': 'testing',
               'DATABASE_URL': 'postgresql+psycopg2://test:test@127.0.0.1:1/pix_test_no_connection',
               'INTERNAL_TOKEN': 'synthetic', 'PIX_POLICY_FILE': str(path)}
        # Import must not connect to this deliberately unreachable database.
        return subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                              text=True, capture_output=True, timeout=15)
