"""
Optional language-model wording for the travel assistant.

It is used only when BOTH hold: an administrator has turned on "let a language model phrase the answers",
and the server has an ANTHROPIC_API_KEY environment variable. The key never goes into the database or the
templates. The model receives the rule-based answer, which is built only from website data, and is told to
keep every fact in it. If the call fails for any reason, the rule-based answer is shown instead.
"""

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

API_URL = 'https://api.anthropic.com/v1/messages'
API_VERSION = '2023-06-01'
TIMEOUT_SECONDS = 15

SYSTEM_PROMPT = (
    'You are the travel assistant for a tour company in Kigali, Rwanda. Rewrite the FACTS below as a short, '
    'friendly reply to the visitor. Use only the facts given. Never add a price, a destination, a date, '
    'a seat count or any other detail that is not in the facts. If the facts do not answer the question, say '
    'so and suggest contacting the team. Keep the same numbers and names exactly. Reply in English.'
)


def can_use():
    return bool(os.environ.get('ANTHROPIC_API_KEY', '').strip())


def phrase(question, facts, model_name):
    """Return the reworded reply, or None when the model is not used or the call fails."""
    key = os.environ.get('ANTHROPIC_API_KEY', '').strip()
    if not key:
        return None
    payload = {
        'model': model_name,
        'max_tokens': 400,
        'system': SYSTEM_PROMPT,
        'messages': [{'role': 'user', 'content': f'FACTS:\n{facts}\n\nVISITOR QUESTION: {question}'}],
    }
    request = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode('utf-8'),
        headers={'content-type': 'application/json', 'x-api-key': key, 'anthropic-version': API_VERSION},
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode('utf-8'))
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        # The reason is logged without the key, the question or the reply.
        logger.warning('Travel assistant language model unavailable: %s', type(error).__name__)
        return None
    text = ''.join(part.get('text', '') for part in body.get('content', []) if part.get('type') == 'text')
    return text.strip() or None
