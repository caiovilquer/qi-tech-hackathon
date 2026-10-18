"""Contract invariants ported from docs/referencia/test_contrato_pix.py (36 tests).

Fixtures ficticias: payload do slide 19 (CPFs com DV correto, sem consulta
de cadastro); CNPJ alfanumerico do exemplo de calculo da Receita Federal.
"""

from copy import deepcopy
from importlib.metadata import version
import json
from pathlib import Path
import unittest
from urllib.parse import quote

from jsonschema import Draft202012Validator, RefResolver, validate
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from antifraud.contract import (
    NormalizedPayload, SCHEMA, ValidationError,
    normalize_json, normalize_payload, parse_payload_json,
)


OFFICIAL_PAYLOAD = {
    "id": "pix-000123",
    "amount": 150000,
    "transaction_date": "2026-10-17T22:15:00-03:00",
    "source_account": {
        "branch": "0001", "account_number": "88213",
        "opening_date": "2025-03-02T10:00:00-03:00",
        "owner": {"document_number": "123.456.789-09"},
    },
    "destination_account": {
        "participant": "60701190", "branch": "3675", "account_number": "10442",
        "opening_date": "2026-10-10T09:00:00-03:00",
        "owner": {"document_number": "987.654.321-00"},
    },
    "source": {
        "device_id": "dvc-9f2c71a8", "channel": "app", "ip": "198.51.100.23",
        "geolocation": {"latitude": -23.56, "longitude": -46.65},
    },
}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.payload = deepcopy(OFFICIAL_PAYLOAD)

    def set_value(self, path, value):
        cursor = self.payload
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor[part]
        cursor[parts[-1]] = value

    def assert_invalid(self, payload=None, status=400, code="QIT000001"):
        with self.assertRaises(ValidationError) as caught:
            normalize_payload(self.payload if payload is None else payload)
        self.assertEqual(caught.exception.http_status, status)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_explicit_draft_without_refs_with_delivered_resolver(self):
        self.assertEqual(version("jsonschema"), "4.17.3")
        self.assertEqual(SCHEMA["$schema"], "https://json-schema.org/draft/2020-12/schema")
        self.assertNotIn('"$ref"', json.dumps(SCHEMA))
        Draft202012Validator.check_schema(SCHEMA)
        resolver = RefResolver(Path(__file__).parent.as_uri() + "/", None)
        validate(self.payload, SCHEMA, resolver=resolver)
        self.payload["amount"] = 150000.0
        validate(self.payload, SCHEMA, resolver=resolver)

    def test_official_payload_and_input_not_mutated(self):
        original = deepcopy(self.payload)
        result = normalize_payload(self.payload)
        self.assertIsInstance(result, NormalizedPayload)
        self.assertEqual(self.payload, original)
        self.assertEqual(result.canonical_payload["transaction_date"], "2026-10-18T01:15:00Z")
        self.assertEqual(result.canonical_payload["source_account"]["owner"]["document_number"], "12345678909")
        self.assertEqual(result.data_quality, ())
        self.assertEqual(len(result.payload_hash), 64)

    def test_only_official_fields_required_and_unknown_fields_rejected(self):
        self.assertEqual(set(SCHEMA["required"]), set(OFFICIAL_PAYLOAD))
        for path in ("extra", "source_account.extra", "destination_account.extra",
                     "source_account.owner.extra", "source.extra", "source.geolocation.extra"):
            with self.subTest(path=path):
                self.payload = deepcopy(OFFICIAL_PAYLOAD)
                self.set_value(path, "x")
                self.assert_invalid()

    def test_missing_fields_and_non_object(self):
        for name in OFFICIAL_PAYLOAD:
            with self.subTest(missing=name):
                payload = deepcopy(OFFICIAL_PAYLOAD)
                del payload[name]
                self.assert_invalid(payload)
        for payload in ([], "payload", 3, True):
            with self.subTest(payload=payload):
                self.assert_invalid(payload)
        with self.assertRaises(ValidationError):
            normalize_payload(None)
        del self.payload["source"]["ip"]
        self.assert_invalid()

    def test_id_accepts_printable_uuid_dots_colon_and_128_characters(self):
        for value in ("pix.001", "pix:001", "pix abc", "ação-1", "x" * 128,
                      "09d9201e-02c8-4eba-a3ee-93a8d463aa21"):
            with self.subTest(value=value):
                self.payload["id"] = value
                self.assertEqual(normalize_payload(self.payload).canonical_payload["id"], value)
        self.assertEqual(quote("pix:001 com espaço", safe=""), "pix%3A001%20com%20espa%C3%A7o")

    def test_id_rejects_reserved_segments_controls_and_delimiters(self):
        for value in ("", "x" * 129, ".", "..", "a/b", "a?b", "a#b", "a\nb", "a\x00b",
                      "a\x7fb", "a\x85b", "a\u200bb", "a\u2028b", 12, None):
            with self.subTest(value=repr(value)):
                self.payload["id"] = value
                self.assert_invalid()

    def test_amount_mathematical_integer_and_bounds(self):
        for value in (1, 150000, 150000.0, 100000000000, 100000000000.0):
            with self.subTest(value=value):
                self.payload["amount"] = value
                self.assertIs(type(normalize_payload(self.payload).canonical_payload["amount"]), int)
        for value in (0, -1, 100000000001, 1.1, 150000.01, True, False,
                      float("nan"), float("inf"), -float("inf"), "150000", None):
            with self.subTest(value=value):
                self.payload["amount"] = value
                self.assert_invalid()

    def test_accounts_ascii_only_and_leading_zeroes_preserved(self):
        for path, values in {
            "source_account.branch": ("00001", "", "١٢", "12\n", "123-", 1),
            "destination_account.branch": ("１２",),
            "source_account.account_number": ("1" * 21, "", "１２３", "12\n", 1),
            "destination_account.participant": ("1234567", "123456789", "１２３４５６７８", "1234-678", "1234567\n"),
        }.items():
            for value in values:
                with self.subTest(path=path, value=value):
                    self.payload = deepcopy(OFFICIAL_PAYLOAD)
                    self.set_value(path, value)
                    self.assert_invalid()
        self.payload = deepcopy(OFFICIAL_PAYLOAD)
        self.payload["source_account"]["account_number"] = "000123"
        self.payload["destination_account"]["participant"] = "A1b2C3d4"
        result = normalize_payload(self.payload).canonical_payload
        self.assertEqual(result["source_account"]["branch"], "0001")
        self.assertEqual(result["source_account"]["account_number"], "000123")
        self.assertEqual(result["destination_account"]["participant"], "A1B2C3D4")

    def test_participant_case_has_same_canonical_value_and_hash(self):
        self.payload["destination_account"]["participant"] = "a1b2c3d4"
        lower = normalize_payload(self.payload)
        self.payload["destination_account"]["participant"] = "A1B2C3D4"
        upper = normalize_payload(self.payload)
        self.assertEqual(lower.canonical_payload["destination_account"]["participant"], "A1B2C3D4")
        self.assertEqual(lower.canonical_payload, upper.canonical_payload)
        self.assertEqual(lower.payload_hash, upper.payload_hash)

    def test_cpf_numeric_and_alphanumeric_cnpj(self):
        for raw, expected in (("529.982.247-25", "52998224725"),
                              ("11.222.333/0001-81", "11222333000181"),
                              ("12.ABC.345/01DE-35", "12ABC34501DE35"),
                              ("12abc34501de35", "12ABC34501DE35")):
            with self.subTest(raw=raw):
                self.payload["source_account"]["owner"]["document_number"] = raw
                result = normalize_payload(self.payload)
                self.assertEqual(result.canonical_payload["source_account"]["owner"]["document_number"], expected)

    def test_document_partial_mask_and_format_are_400(self):
        for raw in ("123456789-09", "123.45678909", "12ABC345/01DE35", "12.ABC.34501DE-35",
                    "123.456.789/09", "123.456.789-09\n", "１２３４５６７８９０９", "12ABC34501DE3A", " 12345678909"):
            with self.subTest(raw=raw):
                self.payload["source_account"]["owner"]["document_number"] = raw
                self.assert_invalid()

    def test_invalid_check_digits_are_422_in_both_accounts(self):
        for account in ("source_account", "destination_account"):
            for raw in ("12345678900", "11111111111", "00000000000", "11222333000182",
                        "00000000000000", "12ABC34501DE34"):
                with self.subTest(account=account, raw=raw):
                    self.payload = deepcopy(OFFICIAL_PAYLOAD)
                    self.payload[account]["owner"]["document_number"] = raw
                    error = self.assert_invalid(status=422, code="QIT002003")
                    self.assertEqual(error.path, f"{account}.owner.document_number")

    def test_timestamp_format_rejects_no_timezone_and_overprecision(self):
        for raw in ("2026-10-17", "2026-10-17T22:15:00", "2026-10-17 22:15:00Z",
                    "2026-10-17T22:15:00.1234567Z", "2026-10-17T22:15:00+0300",
                    "2026-10-17T22:15:00Z\n", "２０２６-10-17T22:15:00Z"):
            with self.subTest(raw=raw):
                self.payload["transaction_date"] = raw
                self.assert_invalid()

    def test_timestamp_semantics_reject_invalid_calendar_clock_and_offset(self):
        for raw in ("2026-02-30T22:15:00Z", "2026-02-29T22:15:00Z", "2026-13-01T22:15:00Z",
                    "2026-10-17T24:00:00Z", "2026-10-17T22:60:00Z", "2026-10-17T22:15:60Z",
                    "2026-10-17T22:15:00+01:60", "2026-10-17T22:15:00-03:99",
                    "2026-10-17T22:15:00+24:00", "0000-01-01T00:00:00Z",
                    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"):
            with self.subTest(raw=raw):
                self.payload["transaction_date"] = raw
                self.assert_invalid(status=422, code="QIT002004")

    def test_opening_cannot_follow_transaction_with_equivalent_offsets(self):
        for account in ("source_account", "destination_account"):
            with self.subTest(account=account):
                self.payload = deepcopy(OFFICIAL_PAYLOAD)
                self.payload[account]["opening_date"] = "2026-10-18T01:15:00.000001Z"
                self.assert_invalid(status=422, code="QIT002005")
                self.payload[account]["opening_date"] = "2026-10-18T01:15:00Z"
                normalize_payload(self.payload)

    def test_timestamp_microseconds_lowercase_and_no_current_time_policy(self):
        self.payload["transaction_date"] = "2099-02-01t12:13:14.123456z"
        result = normalize_payload(self.payload)
        self.assertEqual(result.canonical_payload["transaction_date"], "2099-02-01T12:13:14.123456Z")
        self.payload["transaction_date"] = "2028-02-29T12:00:00Z"
        normalize_payload(self.payload)

    def test_transaction_supported_year_range_is_checked_after_utc_conversion(self):
        for account in ("source_account", "destination_account"):
            self.payload[account]["opening_date"] = "0001-01-01T00:00:00Z"
        for raw in ("0001-01-01T00:00:00Z", "0001-12-31T23:59:59Z",
                    "9999-01-01T00:00:00Z", "9999-12-31T23:59:59.999999Z",
                    "0002-01-01T00:00:00+01:00", "9998-12-31T23:59:59-01:00"):
            with self.subTest(raw=raw):
                self.payload["transaction_date"] = raw
                error = self.assert_invalid(status=422, code="QIT002004")
                self.assertEqual(error.path, "transaction_date")
        for raw, expected in (("0002-01-01T00:00:00Z", "0002-01-01T00:00:00Z"),
                              ("9998-12-31T23:59:59.999999Z", "9998-12-31T23:59:59.999999Z"),
                              ("0001-12-31T23:00:00-01:00", "0002-01-01T00:00:00Z"),
                              ("9999-01-01T00:00:00+01:00", "9998-12-31T23:00:00Z")):
            with self.subTest(raw=raw):
                self.payload["transaction_date"] = raw
                result = normalize_payload(self.payload).canonical_payload
                self.assertEqual(result["transaction_date"], expected)
                self.assertEqual(result["source_account"]["opening_date"], "0001-01-01T00:00:00Z")
        self.payload["transaction_date"] = "9998-12-31T23:59:59Z"
        self.payload["source_account"]["opening_date"] = "9999-01-01T00:00:00Z"
        self.assert_invalid(status=422, code="QIT002005")

    def test_nul_and_isolated_surrogates_rejected_before_normalization(self):
        for field in ("id", "source.device_id", "source.channel", "source.ip"):
            for unsafe in ("a\x00b", "a\ud800b", "a\udfffb"):
                with self.subTest(field=field, unsafe=repr(unsafe)):
                    self.payload = deepcopy(OFFICIAL_PAYLOAD)
                    self.set_value(field, unsafe)
                    error = self.assert_invalid()
                    self.assertEqual(error.path, field)
        for key in ("bad\x00key", "bad\ud800key", "bad\udfffkey"):
            with self.subTest(key=repr(key)):
                self.payload = deepcopy(OFFICIAL_PAYLOAD)
                self.payload["source"][key] = "value"
                error = self.assert_invalid()
                self.assertEqual(error.path, "source.<key>")

    def test_geo_omitted_and_null_equivalent_zero_is_valid(self):
        del self.payload["source"]["geolocation"]
        omitted = normalize_payload(self.payload)
        self.payload["source"]["geolocation"] = None
        self.assertEqual(omitted.payload_hash, normalize_payload(self.payload).payload_hash)
        self.payload["source"]["geolocation"] = {"latitude": 0, "longitude": 0}
        zero = normalize_payload(self.payload)
        self.assertNotEqual(omitted.payload_hash, zero.payload_hash)
        self.payload["source"]["geolocation"] = {"latitude": -0.0, "longitude": 0.0}
        self.assertEqual(zero.payload_hash, normalize_payload(self.payload).payload_hash)

    def test_geo_finite_in_range_complete_and_without_rounding(self):
        for coordinate, invalid_values in (("latitude", (90.1, -90.1)), ("longitude", (180.1, -180.1))):
            for value in (*invalid_values, float("nan"), float("inf"), -float("inf"), True, "0", None):
                with self.subTest(coordinate=coordinate, value=value):
                    self.payload = deepcopy(OFFICIAL_PAYLOAD)
                    self.payload["source"]["geolocation"][coordinate] = value
                    self.assert_invalid()
        self.payload = deepcopy(OFFICIAL_PAYLOAD)
        self.payload["source"]["geolocation"] = {"latitude": -90, "longitude": 180}
        normalize_payload(self.payload)
        del self.payload["source"]["geolocation"]["longitude"]
        self.assert_invalid()
        self.payload["source"]["geolocation"] = {"latitude": -23.56000001, "longitude": -46.65000001}
        result = normalize_payload(self.payload)
        self.assertEqual(result.canonical_payload["source"]["geolocation"], self.payload["source"]["geolocation"])

    def test_ip_required_string_invalid_is_quality_only_and_raw_hash_sensitive(self):
        hashes = set()
        for raw in ("", "invalid", "INVALID", "invalid ", "x" * 80,
                    "256.1.1.1", "198.51.100.23 ", "fe80::1%eth0", "2001:db8::/64"):
            with self.subTest(raw=raw):
                self.payload["source"]["ip"] = raw
                result = normalize_payload(self.payload)
                self.assertIsNone(result.canonical_payload["source"]["ip"])
                self.assertEqual(result.data_quality, ("IP_INVALID",))
                self.assertEqual(result.payload_hash, normalize_payload(deepcopy(self.payload)).payload_hash)
                hashes.add(result.payload_hash)
        self.assertEqual(len(hashes), 9)
        for raw in (None, 123, True, {}):
            with self.subTest(raw=raw):
                self.payload["source"]["ip"] = raw
                self.assert_invalid()

    def test_ipv6_normalizes_but_full_address_remains_in_hash(self):
        self.payload["source"]["ip"] = "2001:0DB8:0000:0000:0000:0000:0000:0001"
        result = normalize_payload(self.payload)
        self.assertEqual(result.canonical_payload["source"]["ip"], "2001:db8::1")
        self.payload["source"]["ip"] = "2001:db8::1"
        self.assertEqual(result.payload_hash, normalize_payload(self.payload).payload_hash)
        self.payload["source"]["ip"] = "2001:db8::2"
        self.assertNotEqual(result.payload_hash, normalize_payload(self.payload).payload_hash)

    def test_equivalent_payloads_same_hash(self):
        original_hash = normalize_payload(self.payload).payload_hash
        self.payload["amount"] = 150000.0
        self.payload["transaction_date"] = "2026-10-18T01:15:00.000000Z"
        self.payload["source_account"]["opening_date"] = "2025-03-02T13:00:00Z"
        self.payload["destination_account"]["opening_date"] = "2026-10-10T12:00:00Z"
        self.payload["source_account"]["owner"]["document_number"] = "12345678909"
        self.payload["destination_account"]["owner"]["document_number"] = "98765432100"
        self.payload["source"]["channel"] = " APP "
        self.payload = dict(reversed(list(self.payload.items())))
        self.payload["source"] = dict(reversed(list(self.payload["source"].items())))
        self.assertEqual(original_hash, normalize_payload(self.payload).payload_hash)

    def test_cnpj_case_and_full_mask_same_hash(self):
        self.payload["source_account"]["owner"]["document_number"] = "12.ABC.345/01DE-35"
        masked = normalize_payload(self.payload).payload_hash
        self.payload["source_account"]["owner"]["document_number"] = "12abc34501de35"
        self.assertEqual(masked, normalize_payload(self.payload).payload_hash)

    def test_material_changes_change_hash(self):
        original_hash = normalize_payload(self.payload).payload_hash
        for path, value in (("id", "pix-000124"), ("amount", 150001),
                            ("transaction_date", "2026-10-18T01:15:00.000001Z"),
                            ("source_account.branch", "001"), ("source_account.account_number", "088213"),
                            ("source.device_id", "DVC-9f2c71a8"), ("source.channel", "web"),
                            ("source.ip", "198.51.100.24"), ("source.geolocation.latitude", -23.56000001)):
            with self.subTest(path=path):
                self.payload = deepcopy(OFFICIAL_PAYLOAD)
                self.set_value(path, value)
                self.assertNotEqual(original_hash, normalize_payload(self.payload).payload_hash)

    def test_channel_blank_rejected_and_device_id_literal_preserved(self):
        self.payload["source"]["channel"] = " \t "
        self.assert_invalid()
        self.payload["source"]["channel"] = "App"
        self.payload["source"]["device_id"] = " AbC "
        result = normalize_payload(self.payload)
        self.assertEqual(result.canonical_payload["source"]["device_id"], " AbC ")
        self.assertEqual(result.canonical_payload["source"]["channel"], "app")

    def test_schema_does_not_mistake_final_newline_for_end_of_string(self):
        for path, value in (("source_account.branch", "1\n"),
                            ("source_account.account_number", "1\n"),
                            ("destination_account.participant", "12345678\n"),
                            ("source_account.owner.document_number", "12345678909\n")):
            with self.subTest(path=path):
                self.payload = deepcopy(OFFICIAL_PAYLOAD)
                self.set_value(path, value)
                with self.assertRaises(JsonSchemaValidationError):
                    validate(self.payload, SCHEMA)


class StrictJsonTests(unittest.TestCase):
    def raw_with_amount(self, token):
        return json.dumps(OFFICIAL_PAYLOAD).replace('"amount": 150000', f'"amount": {token}')

    def assert_bad_json(self, raw):
        with self.assertRaises(ValidationError) as caught:
            normalize_json(raw)
        self.assertEqual(caught.exception.http_status, 400)
        self.assertEqual(caught.exception.code, "QIT000001")

    def test_original_json_bytes_parser_and_wrapper_keep_canonical_hash(self):
        raw = json.dumps(OFFICIAL_PAYLOAD)
        parsed = parse_payload_json(raw)
        self.assertEqual(parsed, OFFICIAL_PAYLOAD)
        # O dict produzido e compativel com o jsonschema da base.
        resolver = RefResolver(Path(__file__).parent.as_uri() + "/", None)
        validate(parsed, SCHEMA, resolver=resolver)
        expected = normalize_payload(OFFICIAL_PAYLOAD)
        self.assertEqual(normalize_json(raw), expected)
        self.assertEqual(normalize_json(raw.encode("utf-8")), expected)

    def test_exact_amount_integer_decimal_exponent_and_bounds(self):
        for token, expected in (("150000", 150000), ("150000.0", 150000),
                                ("150000.0000000000000000000000", 150000),
                                ("1.5e5", 150000), ("1e11", 100000000000), ("1.0", 1)):
            with self.subTest(token=token):
                parsed = parse_payload_json(self.raw_with_amount(token))
                self.assertIs(type(parsed["amount"]), int)
                self.assertEqual(parsed["amount"], expected)
                normalize_payload(parsed)

    def test_fractional_amount_never_disappears_before_validation(self):
        for token in ("150000.00000000001", "100000000000.00000000001",
                      "0.99999999999999999999999", "1e-999999", "1.5", "1e999999", "true", "false"):
            with self.subTest(token=token):
                self.assert_bad_json(self.raw_with_amount(token))
        # Mostra exatamente o bug evitado: o parser padrao perde essa fracao.
        lossy = json.loads(self.raw_with_amount("150000.00000000001"))
        self.assertEqual(lossy["amount"], 150000.0)

    def test_duplicate_keys_at_any_depth_and_escaped_alias_are_rejected(self):
        raw = json.dumps(OFFICIAL_PAYLOAD)
        for duplicate in (
            raw.replace('"amount": 150000', '"amount": 150000, "amount": 150001'),
            raw.replace('"channel": "app"', '"channel": "app", "channel": "web"'),
            raw.replace('"latitude": -23.56', '"latitude": -23.56, "latitude": 0'),
            raw.replace('"amount": 150000', '"amount": 150000, "\\u0061mount": 150000'),
        ):
            with self.subTest(raw=duplicate):
                self.assert_bad_json(duplicate)

    def test_nonfinite_constants_rejected_everywhere(self):
        for token in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(token=token):
                self.assert_bad_json(self.raw_with_amount(token))
                self.assert_bad_json(json.dumps(OFFICIAL_PAYLOAD).replace('"latitude": -23.56', f'"latitude": {token}'))
                self.assert_bad_json('{"unknown": ' + token + '}')

    def test_geo_decimal_bounds_before_float_and_no_precision_loss(self):
        raw = json.dumps(OFFICIAL_PAYLOAD)
        for coordinate, token in (("latitude", "90.00000000000000001"),
                                  ("latitude", "-90.00000000000000001"),
                                  ("longitude", "180.00000000000000001"),
                                  ("longitude", "-180.00000000000000001"),
                                  ("latitude", "-23.5600000000000000000001"),
                                  ("latitude", "1e-999999"), ("latitude", "1e999999")):
            with self.subTest(coordinate=coordinate, token=token):
                old = -23.56 if coordinate == "latitude" else -46.65
                self.assert_bad_json(raw.replace(f'"{coordinate}": {old}', f'"{coordinate}": {token}'))
        for token in ("-90.0", "90.0", "-23.56", "-23.56000001", "0.0", "-0.0"):
            with self.subTest(token=token):
                normalize_json(raw.replace('"latitude": -23.56', f'"latitude": {token}'))

    def test_malformed_encoding_non_object_and_other_types_still_rejected(self):
        for raw in ("{", "[]", "null", '"text"', "2", "true", b"\xff", {}, 3,
                    '{"amount": 1e9999999999999999999}'):
            with self.subTest(raw=raw):
                self.assert_bad_json(raw)
        for path, value in (("id", 1.2), ("source", 1.2)):
            payload = deepcopy(OFFICIAL_PAYLOAD)
            payload[path] = value
            self.assert_bad_json(json.dumps(payload))
        raw = json.dumps(OFFICIAL_PAYLOAD).replace('"latitude": -23.56', '"latitude": true')
        self.assert_bad_json(raw)

    def test_json_escaped_nul_and_surrogates_rejected_in_values_and_keys(self):
        for unsafe in ("a\x00b", "a\ud800b", "a\udfffb"):
            for field in ("device_id", "channel", "ip"):
                with self.subTest(field=field, unsafe=repr(unsafe)):
                    payload = deepcopy(OFFICIAL_PAYLOAD)
                    payload["source"][field] = unsafe
                    self.assert_bad_json(json.dumps(payload))
            with self.subTest(key=repr(unsafe)):
                # Mesmo antes do schema, o parser rejeita a chave nao persistivel.
                with self.assertRaises(ValidationError) as caught:
                    parse_payload_json(json.dumps({unsafe: "value"}))
                self.assertEqual(caught.exception.code, "QIT000001")
                self.assertEqual(caught.exception.http_status, 400)

    def test_valid_unicode_and_json_surrogate_pairs_preserved(self):
        payload = deepcopy(OFFICIAL_PAYLOAD)
        payload["id"] = "pix-😀-ação"
        payload["source"]["device_id"] = "Aparelho 😀"
        payload["source"]["channel"] = " App 😀 "
        payload["source"]["ip"] = "IP inválido 😀"
        escaped = json.dumps(payload, ensure_ascii=True)
        literal = json.dumps(payload, ensure_ascii=False)
        self.assertIn("\\ud83d\\ude00", escaped)
        result = normalize_json(escaped)
        self.assertEqual(result, normalize_json(literal.encode("utf-8")))
        self.assertEqual(result.canonical_payload["source"]["device_id"], "Aparelho 😀")
        self.assertEqual(result.canonical_payload["source"]["channel"], "app 😀")
        self.assertEqual(result.data_quality, ("IP_INVALID",))
        self.assertEqual(parse_payload_json('{"\\ud83d\\ude00": "válido"}'), {"😀": "válido"})


    def test_error_path_names_non_finite_and_extra_fields(self):
        raw = json.dumps(OFFICIAL_PAYLOAD)[:-1] + ', "ratio": NaN}'
        with self.assertRaises(ValidationError) as caught:
            parse_payload_json(raw)
        self.assertEqual(caught.exception.path, "ratio")
        payload = deepcopy(OFFICIAL_PAYLOAD)
        payload["source"]["unexpected"] = 1
        with self.assertRaises(ValidationError) as caught:
            normalize_json(json.dumps(payload))
        self.assertEqual(caught.exception.path, "source.unexpected")

if __name__ == "__main__":
    unittest.main()
