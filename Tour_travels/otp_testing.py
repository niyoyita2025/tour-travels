"""Helpers for the tests: read the code from an emailed message and type it into the six code boxes.

Not named test*.py on purpose, so the test runner does not collect it as a test module.
"""

import re


def emailed_code(message):
    match = re.search(r'^(\d{6})$', message.body, re.MULTILINE)
    if match is None:
        raise AssertionError('The email does not contain a six-digit code.')
    return match.group(1)


def code_post(code):
    """POST data for the six code boxes, exactly as the code page submits them."""
    return {f'digit_{index}': digit for index, digit in enumerate(code)}
