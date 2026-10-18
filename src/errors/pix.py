"""Pix errors retain the Base API envelope. Descriptions contain no raw payload."""
from errors.base_error import QIException


class PixError(QIException):
    code = 'QIT002008'

    def __init__(self, status=503, code='QIT002008'):
        descriptions = {
            'QIT002001': 'Pix analysis not found in this institution.',
            'QIT002002': 'This Pix ID is permanently reserved with another payload.',
            'QIT002003': 'Invalid document check digits.',
            'QIT002004': 'Invalid date, time or timezone offset.',
            'QIT002005': 'Account opening follows the transaction.',
            'QIT002009': 'Request body exceeds 65536 bytes.',
            'QIT000001': 'Invalid Pix JSON, structure, type or format.',
            'QIT002008': 'Reliable result unavailable. Retry the same ID and body.',
        }
        super().__init__('Pix request error',code,status,descriptions[code],
                         'Consulte o código do erro; em falha técnica, repita o mesmo ID e corpo.')
