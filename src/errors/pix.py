"""Pix error catalogue in the Base API envelope. Descriptions never echo payload values."""
from errors.base_error import QIException

# code: (HTTP status, title, description, translation)
PIX_ERRORS = {
    'QIT000001': (400, 'Invalid Pix payload',
                  'Invalid Pix JSON, structure, type or format.',
                  'Corpo do Pix inválido: JSON, estrutura, tipo ou formato.'),
    'QIT002001': (404, 'Pix analysis not found',
                  'Pix analysis not found in this institution.',
                  'Análise Pix não encontrada nesta instituição.'),
    'QIT002002': (409, 'Pix ID already reserved',
                  'This Pix ID is permanently reserved with another payload.',
                  'Este ID de Pix já está reservado com outro conteúdo.'),
    'QIT002003': (422, 'Invalid document',
                  'Invalid document check digits.',
                  'CPF/CNPJ com dígitos verificadores inválidos.'),
    'QIT002004': (422, 'Invalid timestamp',
                  'Invalid date, time or timezone offset.',
                  'Data, hora ou fuso horário inválidos.'),
    'QIT002005': (422, 'Account opened after transaction',
                  'Account opening follows the transaction.',
                  'A abertura da conta é posterior à transação.'),
    'QIT002008': (503, 'Pix analysis unavailable',
                  'Reliable result unavailable. Retry the same ID and body.',
                  'Falha técnica: repita a requisição com o mesmo ID e corpo.'),
    'QIT002009': (413, 'Pix payload too large',
                  'Request body exceeds 65536 bytes.',
                  'Corpo da requisição acima de 65536 bytes.'),
    'QIT002010': (409, 'Reconciliation required',
                  'Institution reconciliation required for account ownership or bank identity. Do not retry until repaired.',
                  'É necessária reconciliação de titularidade ou vínculo bancário pela instituição antes de repetir.'),
}


class PixError(QIException):
    code = 'QIT002008'

    def __init__(self, status=503, code='QIT002008', field=None):
        expected_status, title, description, translation = PIX_ERRORS[code]
        if status != expected_status:
            raise ValueError(f'Pix error {code} is always HTTP {expected_status}')
        if field:
            description = f'{description} Field: {field}.'
        super().__init__(title, code, status, description, translation)
