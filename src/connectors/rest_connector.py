import json
import math
import time
from abc import ABCMeta

import requests

from utils.logger import get_logger, log_fields


class RestConnector(metaclass=ABCMeta):
    """How this service calls another HTTP service: configured base URL, mandatory timeout, logged round trips.

    Responses are returned as they came; deciding what a status means belongs to the controller.
    """

    def __init__(self, class_name: str, base_url: str, timeout: float, internal_token: str) -> None:
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Connector timeout must be positive finite seconds")
        self.logger = get_logger(class_name)
        self.base_url = base_url
        self.timeout = float(timeout)
        self.internal_token = internal_token

    def send(self, endpoint: str, method: str, payload: dict = None, headers: dict = None):
        if headers is None:
            headers = {}
        headers["INTERNAL-TOKEN"] = self.internal_token
        url = f"{self.base_url}{endpoint}"
        self.logger.info("outgoing.started " + log_fields(method=method, url=url))
        started_at = time.perf_counter()
        response = requests.request(method.upper(), url, json=payload, headers=headers, timeout=self.timeout)
        elapsed_ms = (time.perf_counter() - started_at) * 1000
        self.logger.info("outgoing.finished " + log_fields(method=method, url=url, status=response.status_code,
                                                           elapsed_ms=elapsed_ms))
        return BaseConnectorResponse(endpoint, method, response)


class BaseConnectorResponse:
    """Status and decoded JSON body (None when the body is not JSON), plus what was requested."""

    def __init__(self, endpoint: str, method: str, response: requests.Response) -> None:
        self.endpoint = endpoint
        self.method = method
        self.status = response.status_code
        self.json = None
        try:
            self.json = json.loads(response.content)
        except ValueError:
            pass
