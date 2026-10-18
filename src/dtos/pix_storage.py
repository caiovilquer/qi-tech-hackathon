"""Typed internal storage outcomes; the HTTP mapping belongs to the next stage."""
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
import re

from antifraud.context import AccountKey, supported_time
from antifraud.dto import PixTransactionDTO
from providers.contracts import account, ProviderIdentity


class Busy(RuntimeError):
    """A live owner holds or is finalizing the reservation; retry within the existing deadline."""

class LostClaim(RuntimeError):
    """Lease expired, ownership replaced or initial check not completed."""

class HashConflict(ValueError):
    code='QIT002002'

class OwnershipConflict(ValueError):
    """Explicit reconciliation required; never pool account owners."""

class EventConflict(ValueError):
    code='QIT002006'

class InvalidEvent(ValueError):
    code='QIT002007'

class RecordConflict(ValueError):
    """Two different facts claimed the same provider source revision."""

@dataclass(frozen=True)
class ClaimTicket:
    tenant: str
    external_id: str
    payload_hash: str
    owner: UUID
    fence: int
    lease_until: datetime | None=None  # PostgreSQL clock at reservation; the row stays the authority

@dataclass(frozen=True)
class InitialTicket:
    claim: ClaimTicket
    payer: AccountKey
    owner: UUID
    fence: int
    lease_until: datetime | None=None

@dataclass(frozen=True)
class Replay:
    dto: PixTransactionDTO

@dataclass(frozen=True)
class EventInput:
    tenant: str
    provider: str
    event_id: str
    kind: str
    payer: AccountKey
    external_id: str
    analysis_key: str
    operation_fingerprint: str
    state: str
    occurred_at: datetime
    source_version: int=0
    challenge_id: str | None=None
    terminal: bool=True

    def __post_init__(self):
        # Relational/terminal validation occurs inside the locked transaction.
        try:
            account(self.payer)
            ProviderIdentity(self.provider,'v1')
            supported_time(self.occurred_at,'bank event occurrence')
            if not isinstance(self.kind,str) or not isinstance(self.state,str):
                raise ValueError('Event kind/state must be strings')
            if type(self.source_version) is not int or not 0<=self.source_version<2**63 or type(self.terminal) is not bool:
                raise ValueError('Invalid source version/terminal flag')
            if self.challenge_id is not None and (not isinstance(self.challenge_id,str) or not self.challenge_id.strip() or not self.challenge_id.isprintable() or len(self.challenge_id)>128):
                raise ValueError('Invalid challenge ID')
            for value in (self.event_id,self.external_id,self.analysis_key,self.operation_fingerprint):
                if not isinstance(value,str) or not value.strip() or not value.isprintable() or len(value)>128:
                    raise ValueError('Invalid event identity')
            object.__setattr__(self,'analysis_key',str(UUID(self.analysis_key)))
            if re.fullmatch(r'[a-f0-9]{64}',self.operation_fingerprint) is None:
                raise ValueError('Invalid financial fingerprint')
        except (TypeError,ValueError,AttributeError,OverflowError) as error:
            raise InvalidEvent(str(error)) from error
