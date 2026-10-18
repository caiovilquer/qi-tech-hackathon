"""Monotonic request budget and cooperative async provider cancellation."""
import asyncio
from dataclasses import dataclass, field
import math
from socket import gaierror
from time import monotonic
from typing import Callable

from .contracts import ProviderUnavailable


class BudgetExceeded(Exception):
    """Request budget exhausted; do not produce a decision from a late result."""


def positive_seconds(value):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError('Timeout must be positive finite seconds')
    return float(value)


@dataclass(frozen=True)
class Deadline:
    expires_at: float
    clock: Callable[[], float] = field(default=monotonic, repr=False, compare=False)

    def __post_init__(self):
        if type(self.expires_at) not in (int, float) or not math.isfinite(self.expires_at) or not callable(self.clock):
            raise ValueError('Invalid monotonic deadline')

    @classmethod
    def after(cls, seconds=3, *, clock=monotonic):
        return cls(clock()+positive_seconds(seconds), clock)

    def remaining(self):
        remaining = self.expires_at-self.clock()
        if remaining <= 0:
            raise BudgetExceeded('Request deadline exceeded')
        return remaining


@dataclass(frozen=True)
class ProviderRunner:
    call_seconds: float = .25
    attempts: int = 2

    def __post_init__(self):
        object.__setattr__(self, 'call_seconds', positive_seconds(self.call_seconds))
        if type(self.attempts) is not int or self.attempts not in (1, 2):
            raise ValueError('At most one technical retry is allowed')

    async def call(self, operation, deadline: Deadline):
        for attempt in range(self.attempts):
            seconds = min(self.call_seconds, deadline.remaining())
            call_expires = deadline.clock()+seconds
            try:
                value = await asyncio.wait_for(operation(seconds), timeout=seconds)
                deadline.remaining()
                if deadline.clock() >= call_expires:
                    raise TimeoutError('Provider returned after call deadline')
                return value
            except (ConnectionError, TimeoutError, gaierror, ProviderUnavailable) as error:
                deadline.remaining()
                if attempt == self.attempts-1:
                    raise ProviderUnavailable('Provider technical attempts exhausted') from error
        raise AssertionError('Validated attempts must execute')
