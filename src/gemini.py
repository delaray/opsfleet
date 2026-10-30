import json
import logging
import os
import random
import time
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv

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


# -----------------------------------------------------------------------------
# Query Gemini API
# -----------------------------------------------------------------------------

def query_gemini(prompt: str, *, model: str,
                 timeout: float = 60,
                 max_retries: int = 3
                 ) -> str:
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

    model = quote(model.removeprefix("models/"), safe="")
    googleapi_url_prefix = "https://generativelanguage.googleapis.com/"
    request = Request(
        f"{googleapi_url_prefix}v1beta/models/{model}:generateContent",
        data=json.dumps({"contents": [{"parts": [{"text": prompt}]}]}).encode(),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": GEMINI_API_KEY,
        },
        method="POST",
    )
    for attempt in range(max_retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                result = json.load(response)
            break
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            exc.close()
            if exc.code in {408, 429, 500, 502, 503, 504} and attempt < max_retries:
                delay = 2 ** min(attempt, 5) + random.uniform(0, 1)
                logger.warning(
                    "Gemini HTTP %s; retry %s/%s in %.1fs",
                    exc.code, attempt + 1, max_retries, delay,
                )
                time.sleep(delay)
                continue
            try:
                message = json.loads(body).get("error", {}).get("message") or exc.reason
            except (ValueError, AttributeError):
                message = exc.reason
            message = str(message).replace(GEMINI_API_KEY, "[REDACTED]")
            raise RuntimeError(
                f"Gemini API error {exc.code} after {attempt + 1} attempt(s): {message}"
            ) from None

    candidates = result.get("candidates", [])
    parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
    text = "".join(
        part["text"] for part in parts if "text" in part and not part.get("thought")
    )
    if not text:
        raise RuntimeError("Gemini returned no generated text")
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
