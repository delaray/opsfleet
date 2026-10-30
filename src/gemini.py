import json
import logging
import os
import random
import re
import time
import datetime as dt
from email.utils import parsedate_to_datetime
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen
from urllib.error import URLError

from dotenv import load_dotenv
if __package__:
    from .telemetry import BudgetExceeded, request_scope
else:  # Preserve `python src/gemini.py` smoke-test usage.
    from telemetry import BudgetExceeded, request_scope

logger = logging.getLogger("opsfleet-logger")

# -----------------------------------------------------------------------------
# Load environment variables from .env file
# -----------------------------------------------------------------------------

load_dotenv(override=True)

PROJECT_NAME = os.getenv("PROJECT_NAME")
PROJECT_ID = os.getenv("PROJECT_ID")
DATASET_ID = os.getenv("DATASET_ID")    

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL_1 = os.getenv("GEMINI_MODEL_1")
GEMINI_MODEL_2 = os.getenv("GEMINI_MODEL_2")
DEFAULT_MODEL = "gemini-3.6-flash"


class GeminiAPIError(RuntimeError):
    """HTTP failure with a status code callers can safely handle without payloads."""

    def __init__(self, status_code: int, message: str, *, reason: str | None = None):
        self.status_code = status_code
        self.reason = reason or ('rate_limit' if status_code == 429 else 'http_error')
        super().__init__(message)


# -----------------------------------------------------------------------------
# Query Gemini API
# -----------------------------------------------------------------------------

def query_gemini(prompt: str, *, model: str,
                 timeout: float = 60,
                 max_retries: int = 3
                 ) -> str:
    """Query Gemini within the current request budget, or a standalone default one."""
    with request_scope(kind='gemini') as runtime:
        try:
            return _query_gemini(prompt, model=model, timeout=timeout, max_retries=max_retries, runtime=runtime)
        except BudgetExceeded:
            runtime.emit('model_finished', status='error', category='budget_exceeded')
            raise


def _query_gemini(prompt, *, model, timeout, max_retries, runtime):
    """
    Query the specified Gemini model and return its generated text.

    Raises ValueError for missing configuration or an empty prompt, and
    RuntimeError for API errors or if the model returns no text.
    Transient HTTP errors are retried up to max_retries times with exponential
    backoff and jitter. timeout applies to each attempt.
    Network errors propagate.
    """
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY is not configured")
    if not model:
        raise ValueError("model is not configured")
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    if not isinstance(max_retries, int) or max_retries < 0:
        raise ValueError("max_retries must be a non-negative integer")
    if timeout <= 0:
        raise ValueError('timeout must be positive')

    runtime.model_call(model)
    call_started = runtime.clock()
    model = quote(model.removeprefix("models/"), safe="")
    googleapi_url_prefix = "https://generativelanguage.googleapis.com/"
    request = Request(
        f"{googleapi_url_prefix}v1beta/models/{model}:generateContent",
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}],
                         "generationConfig": {"maxOutputTokens": runtime.limits.max_output_tokens}}).encode(),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        method="POST",
    )
    for attempt in range(max_retries + 1):
        runtime.model_attempt(retry=attempt > 0)
        attempt_started = runtime.clock()
        try:
            with urlopen(request, timeout=runtime.remaining(timeout)) as response:
                encoded = response.read(1_000_001)
                if len(encoded) > 1_000_000:
                    raise ValueError('Gemini response exceeds size budget')
                result = json.loads(encoded)
            runtime.remaining()
            runtime.record_usage(result.get('usageMetadata'))
            runtime.emit('model_attempt_finished', status='success',
                         duration_ms=max(0, (runtime.clock() - attempt_started) * 1000))
            break
        except HTTPError as exc:
            body = exc.read(64_001).decode("utf-8", errors="replace")
            exc.close()
            try:
                error = json.loads(body).get('error', {})
                message = error.get('message') or exc.reason
                details = error.get('details', [])
            except (ValueError, AttributeError):
                message, details = exc.reason, []
            # Classify quota metadata, not model/user text in normal responses.
            quota = json.dumps(details).lower() + ' ' + str(message).lower()
            daily = exc.code == 429 and any(word in quota for word in ('perday', 'per_day', 'per day', 'daily'))
            reason = ('daily_quota' if daily else 'rate_limit') if exc.code == 429 else (
                'overloaded' if exc.code in {500, 502, 503, 504} else 'http_error')
            hint = 0.0
            header = exc.headers.get('Retry-After') if exc.headers else None
            if header and re.fullmatch(r'\d+(?:\.\d+)?', header):
                hint = float(header)
            elif header:
                try:
                    when = parsedate_to_datetime(header)
                    if when.tzinfo is None:
                        when = when.replace(tzinfo=dt.timezone.utc)
                    hint = max(0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())
                except (TypeError, ValueError, OverflowError):
                    pass
            if isinstance(details, list):
                for detail in details:
                    if isinstance(detail, dict) and str(detail.get('@type', '')).endswith('google.rpc.RetryInfo'):
                        match = re.fullmatch(r'(\d+(?:\.\d+)?)s', str(detail.get('retryDelay', '')))
                        if match:
                            hint = max(hint, float(match[1]))
            runtime.emit('model_attempt_finished', status='error', category='gemini_' + reason,
                         http_status=exc.code, retry_after_seconds=hint,
                         duration_ms=max(0, (runtime.clock() - attempt_started) * 1000))
            if not daily and exc.code in {408, 429, 500, 502, 503, 504} and attempt < max_retries and hint <= 30:
                runtime.require_retry_capacity()
                delay = max(hint, 2 ** min(attempt, 5) + random.uniform(0, 1))
                if runtime.remaining() <= delay:
                    runtime._exceeded('retry_deadline')
                runtime.emit('model_retry', category='gemini_' + reason, retry_delay_seconds=delay)
                logger.warning(
                    "Gemini HTTP %s; retry %s/%s in %.1fs",
                    exc.code, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                continue
            runtime.fail('gemini_' + reason)
            runtime.emit('model_finished', status='error', category='gemini_' + reason,
                         duration_ms=max(0, (runtime.clock() - call_started) * 1000))
            message = str(message).replace(GEMINI_API_KEY, "[REDACTED]")
            raise GeminiAPIError(exc.code,
                f"Gemini API error {exc.code} after {attempt + 1} attempt(s): {message}", reason=reason
            ) from None
        except (URLError, TimeoutError):
            runtime.fail('model_network_error')
            runtime.emit('model_finished', status='error', category='model_network_error',
                         duration_ms=max(0, (runtime.clock() - call_started) * 1000))
            raise
        except (ValueError, AttributeError, TypeError):
            runtime.fail('model_response_error')
            runtime.emit('model_finished', status='error', category='model_response_error',
                         duration_ms=max(0, (runtime.clock() - call_started) * 1000))
            raise RuntimeError('Gemini returned an invalid or oversized response') from None

    candidates = result.get("candidates", [])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "".join(
        part["text"] for part in parts if "text" in part and not part.get("thought")
    )
    if not text:
        runtime.fail('model_empty_response')
        raise RuntimeError("Gemini returned no generated text")
    runtime.remaining()
    runtime.emit('model_finished', status='success', duration_ms=max(0, (runtime.clock() - call_started) * 1000))
    return text


def test_query_gemini(model=DEFAULT_MODEL,
                      prompt="What is the capital of France?"
                      ) -> None:
    """
    Smoke-test the configured model with a simple question.
    """
    answer = query_gemini(prompt, model=model)
    print(answer)
    assert "paris" in answer.casefold(), f"Expected answer Paris: {answer!r}"


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    test_query_gemini()
