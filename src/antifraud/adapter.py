"""Pure conversion of a validated official payload; tenant comes from the server."""
import re
from datetime import datetime

from .context import AccountKey, DestinationKey, Pix
from .contract import NormalizedPayload


def to_pix(normalized: NormalizedPayload, tenant: str) -> Pix:
    if not isinstance(tenant, str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", tenant) is None:
        raise ValueError("Invalid server tenant identity")
    payload = normalized.canonical_payload
    payer, payee = payload["source_account"], payload["destination_account"]
    return Pix(
        id=payload["id"],
        payer=AccountKey(tenant, payer["branch"], payer["account_number"], payer["owner"]["document_number"]),
        payee_document=payee["owner"]["document_number"],
        destination=DestinationKey(payee["participant"], payee["branch"], payee["account_number"]),
        amount_cents=payload["amount"],
        transaction_at=datetime.fromisoformat(payload["transaction_date"].replace("Z", "+00:00")),
        destination_opened_at=datetime.fromisoformat(payee["opening_date"].replace("Z", "+00:00")),
        device_id=payload["source"]["device_id"],
    )
