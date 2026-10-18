CPF_LENGTH = 11
CHECK_DIGIT_POSITIONS = [9, 10]


def is_valid_cpf(document_number: str) -> bool:
    """Check digits of a CPF (the schema already enforced the format).

    Each check digit is the weighted sum of the previous digits modulo 11: remainder below 2
    gives 0, otherwise 11 minus the remainder. Repeated-digit numbers pass that arithmetic but
    are never issued, so they are refused as well.
    """
    digits = [int(character) for character in document_number if character.isdigit()]
    if len(digits) != CPF_LENGTH:
        return False
    if all(digit == digits[0] for digit in digits):
        return False
    for position in CHECK_DIGIT_POSITIONS:
        first_weight = position + 1
        total = sum(digits[index] * (first_weight - index) for index in range(position))
        remainder = total % 11
        expected_digit = 0 if remainder < 2 else 11 - remainder
        if digits[position] != expected_digit:
            return False
    return True
