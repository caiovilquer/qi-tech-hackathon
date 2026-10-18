"""The original connector must preserve positive fractional socket timeouts."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from connectors.rest_connector import RestConnector

ROOT = Path(__file__).resolve().parents[2]


class ConnectorTimeoutTests(unittest.TestCase):
    def test_fractional_timeout_reaches_requests_without_rounding(self):
        connector = RestConnector('test', 'https://example.invalid', .25, 'synthetic-token')
        response = Mock(status_code=200, content=b'{}')
        with patch('connectors.rest_connector.requests.request', return_value=response) as request:
            connector.send('/demo', 'GET')
        self.assertEqual(request.call_args.kwargs['timeout'], .25)

    def test_connector_rejects_invalid_timeouts(self):
        for value in (0, -.1, True, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                RestConnector('test', 'https://example.invalid', value, 'synthetic-token')

    def test_env_accepts_fractional_timeout_and_refuses_nonpositive_or_nonfinite(self):
        for value in ('.25', '0', '-1', 'nan', 'inf'):
            env = {**os.environ, 'PYTHONPATH': str(ROOT/'src'), 'BANKSLIP_API_TIMEOUT': value}
            code = 'from constants import BANKSLIP_API_TIMEOUT'
            if value == '.25':
                code += '; assert BANKSLIP_API_TIMEOUT == .25'
            result = subprocess.run([sys.executable, '-c', code],
                                    env=env, cwd=ROOT, capture_output=True, text=True, timeout=10)
            with self.subTest(value=value):
                self.assertEqual(result.returncode==0, value=='.25', result.stderr)
