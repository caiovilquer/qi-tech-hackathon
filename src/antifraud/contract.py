"""Official Pix boundary: strict decimal JSON, validation and canonical SHA-256.

Ported from the V3 reference. Read raw HTTP bytes before calling normalize_json;
body-size enforcement belongs to the future resource. No HTTP route lives here.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from hashlib import sha256
from ipaddress import ip_address
import json
import math
from pathlib import Path
import re
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError


SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "post_pix_transaction.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
Draft202012Validator.check_schema(SCHEMA)
_VALIDATOR = Draft202012Validator(SCHEMA)
_TIMESTAMP = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?P<offset>[Zz]|[+-][0-9]{2}:[0-9]{2})"
)
_CNPJ_WEIGHTS = (
    (5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
    (6, 5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2),
)


class ValidationError(ValueError):
    """Erro de entrada; http_status e code podem ser mapeados pela API."""

    def __init__(self, http_status: int, code: str, message: str, path: str = ""):
        self.http_status = http_status
        self.code = code
        self.path = path
        super().__init__(f"{path}: {message}" if path else message)


@dataclass(frozen=True)
class NormalizedPayload:
    """Payload proprio para o motor, hash de idempotencia e qualidade."""

    _payload_json: str
    payload_hash: str
    data_quality: tuple[str, ...]

    @property
    def canonical_payload(self) -> dict[str, Any]:
        # Callers receive an independent JSON value, never mutable backing state.
        return json.loads(self._payload_json)


def _check_jsonb_text(value: str, path: str) -> None:
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise ValidationError(
            400, "QIT000001", "String contem NUL ou surrogate isolado.", path
        )


def _validate_jsonb_strings(payload: Any) -> None:
    pending = [(payload, "")]
    visited = set()
    while pending:
        value, path = pending.pop()
        if isinstance(value, str):
            _check_jsonb_text(value, path)
        elif isinstance(value, (dict, list)):
            if id(value) in visited:
                continue
            visited.add(id(value))
            if isinstance(value, dict):
                for key, child in value.items():
                    key_path = f"{path}.<key>" if path else "<key>"
                    if not isinstance(key, str):
                        raise ValidationError(400, "QIT000001", "Chave JSON deve ser string.", key_path)
                    _check_jsonb_text(key, key_path)
                    pending.append((child, f"{path}.{key}" if path else key))
            else:
                pending.extend((child, f"{path}[{index}]") for index, child in enumerate(value))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError(400, "QIT000001", "Chave JSON duplicada.", key)
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ValidationError(400, "QIT000001", f"Constante JSON nao finita: {value}.")


def parse_payload_json(raw: str | bytes) -> dict[str, Any]:
    """Le JSON estrito sem perder fracao de amount antes da validacao.

    Retorna dict a ser passado a normalize_payload (o schema continua
    responsavel por campos, tipos e estrutura). Amount decimal somente
    vira int depois de ser integral e estar no intervalo permitido.
    Coordenadas so viram float depois de conferir finitude, intervalo e
    preservacao do valor decimal por Decimal(str(float(valor))). Isso
    recusa, por exemplo, latitude 90.00000000000000001 antes que vire 90.

    Decimais em outros campos permanecem Decimal para que o schema os
    rejeite pelo tipo/estrutura; nenhum campo novo e criado no payload.
    """
    if not isinstance(raw, (str, bytes)):
        raise ValidationError(400, "QIT000001", "Corpo JSON deve ser texto ou bytes.")
    try:
        payload = json.loads(
            raw, parse_int=int, parse_float=Decimal,
            parse_constant=_reject_constant, object_pairs_hook=_unique_object,
        )
    except ValidationError:
        raise
    except (ValueError, UnicodeError, DecimalException, RecursionError) as error:
        raise ValidationError(400, "QIT000001", "Corpo JSON invalido.") from error
    if not isinstance(payload, dict):
        raise ValidationError(400, "QIT000001", "Corpo JSON deve ser objeto.")
    _validate_jsonb_strings(payload)

    amount = payload.get("amount")
    if isinstance(amount, Decimal):
        if (not amount.is_finite() or not 1 <= amount <= 100000000000
                or amount != amount.to_integral_value()):
            raise ValidationError(
                400, "QIT000001", "Centavos devem ser inteiros entre 1 e 100000000000.", "amount"
            )
        payload["amount"] = int(amount)

    source = payload.get("source")
    geolocation = source.get("geolocation") if isinstance(source, dict) else None
    if isinstance(geolocation, dict):
        for coordinate, limit in (("latitude", 90), ("longitude", 180)):
            value = geolocation.get(coordinate)
            if isinstance(value, Decimal):
                path = f"source.geolocation.{coordinate}"
                if not value.is_finite() or not -limit <= value <= limit:
                    raise ValidationError(400, "QIT000001", "Coordenada fora do intervalo.", path)
                converted = float(value)
                if not math.isfinite(converted) or Decimal(str(converted)) != value:
                    raise ValidationError(
                        400, "QIT000001", "Precisao da coordenada nao representavel sem perda.", path
                    )
                geolocation[coordinate] = converted
    return payload


def normalize_json(raw: str | bytes) -> NormalizedPayload:
    """Entrada recomendada para o corpo HTTP bruto: parse estrito e normalizacao."""
    return normalize_payload(parse_payload_json(raw))


def _document(raw: str, path: str) -> str:
    # O schema ja exigiu uma mascara completa ou nenhum separador.
    normalized = raw.translate(str.maketrans("", "", ".-/")).upper()
    values = [ord(char) - 48 for char in normalized]
    valid = len(set(normalized)) > 1
    if len(normalized) == 11:
        checks = ((9, range(10, 1, -1)), (10, range(11, 1, -1)))
    else:
        checks = zip((12, 13), _CNPJ_WEIGHTS)
    for position, weights in checks:
        remainder = sum(value * weight for value, weight in zip(values, weights)) % 11
        valid = valid and values[position] == (0 if remainder < 2 else 11 - remainder)
    if not valid:
        raise ValidationError(
            422, "QIT002003", "CPF/CNPJ com DV invalido.", path
        )
    return normalized


def _timestamp(raw: str, path: str) -> datetime:
    match = _TIMESTAMP.fullmatch(raw)
    if match is None:
        raise ValidationError(400, "QIT000001", "Timestamp fora do formato.", path)
    offset = match.group("offset")
    # fromisoformat aceita e carrega minutos como +01:60 para +02:00.
    # Essa permissividade nao pode transformar uma data invalida em replay.
    if offset.upper() != "Z" and (int(offset[1:3]) > 23 or int(offset[4:6]) > 59):
        raise ValidationError(422, "QIT002004", "Offset invalido.", path)
    try:
        return datetime.fromisoformat(raw.upper().replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except (ValueError, OverflowError) as error:
        raise ValidationError(
            422, "QIT002004", "Calendario ou instante UTC invalido.", path
        ) from error


def _utc_text(value: datetime) -> str:
    # isoformat preserva integralmente a precisao aceita de microssegundos.
    return value.isoformat().replace("+00:00", "Z")


def normalize_payload(payload: dict[str, Any]) -> NormalizedPayload:
    """Valida o contrato, normaliza uma copia e calcula seu hash SHA-256.

    Formato/estrutura/tipo/dados nao finitos: QIT000001/400. DV invalido:
    QIT002003/422. Calendario/offset invalido: QIT002004/422. Abertura
    posterior a transacao: QIT002005/422. IP malformado continua aceito,
    com IP_INVALID e None.
    Uma vez validado, amount e convertido para int; bool e fracao nao sao
    convertidos. A entrada deve usar tipos JSON usuais (dict/list/str/
    int/float/bool/None). Em HTTP, use parse_payload_json/normalize_json:
    o json.loads padrao pode perder uma fracao antes de chegarmos aqui.
    """
    _validate_jsonb_strings(payload)
    try:
        _VALIDATOR.validate(payload)
    except JsonSchemaValidationError as error:
        path = ".".join(str(part) for part in error.absolute_path)
        raise ValidationError(400, "QIT000001", error.message, path) from error

    if not payload["id"].isprintable():
        raise ValidationError(400, "QIT000001", "ID deve ser imprimivel.", "id")

    amount = payload["amount"]
    # Protecao explicita anterior a int(): nunca bool, NaN, infinito ou fracao.
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise ValidationError(400, "QIT000001", "Centavos devem ser inteiros.", "amount")
    if isinstance(amount, float) and (not math.isfinite(amount) or not amount.is_integer()):
        raise ValidationError(400, "QIT000001", "Centavos devem ser inteiros finitos.", "amount")

    canonical = deepcopy(payload)
    canonical["amount"] = int(amount)
    canonical["destination_account"]["participant"] = canonical["destination_account"]["participant"].upper()
    transaction_at = _timestamp(payload["transaction_date"], "transaction_date")
    if not 2 <= transaction_at.year <= 9998:
        raise ValidationError(
            422, "QIT002004", "Ano UTC da transacao deve estar entre 0002 e 9998.", "transaction_date"
        )
    canonical["transaction_date"] = _utc_text(transaction_at)

    for account_name in ("source_account", "destination_account"):
        account = canonical[account_name]
        opening_at = _timestamp(account["opening_date"], f"{account_name}.opening_date")
        if opening_at > transaction_at:
            raise ValidationError(
                422,
                "QIT002005",
                "Abertura nao pode ser posterior a transacao.",
                f"{account_name}.opening_date",
            )
        account["opening_date"] = _utc_text(opening_at)
        account["owner"]["document_number"] = _document(
            account["owner"]["document_number"], f"{account_name}.owner.document_number"
        )

    source = canonical["source"]
    source["channel"] = source["channel"].strip().lower()
    if not source["channel"]:
        raise ValidationError(400, "QIT000001", "Canal vazio apos strip.", "source.channel")

    geolocation = source.setdefault("geolocation", None)
    if geolocation is not None:
        for coordinate in ("latitude", "longitude"):
            value = geolocation[coordinate]
            if not math.isfinite(value):
                raise ValidationError(
                    400, "QIT000001", "Coordenada deve ser finita.",
                    f"source.geolocation.{coordinate}",
                )
            # Uniformiza 0/0.0/-0.0 e outros inteiros matematicos sem round().
            if isinstance(value, float) and value.is_integer():
                geolocation[coordinate] = int(value)

    invalid_ip_raw = None
    quality: tuple[str, ...] = ()
    try:
        # Zona de interface (%eth0), mascara e espacos nao sao um IP puro.
        if "%" in source["ip"]:
            raise ValueError("IP com zona de interface")
        source["ip"] = str(ip_address(source["ip"]))
    except ValueError:
        invalid_ip_raw = source["ip"]
        source["ip"] = None
        quality = ("IP_INVALID",)

    envelope = {"payload": canonical, "invalid_ip_raw": invalid_ip_raw}
    canonical_json = json.dumps(
        envelope, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return NormalizedPayload(
        json.dumps(canonical, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False),
        sha256(canonical_json.encode("utf-8")).hexdigest(), quality,
    )
