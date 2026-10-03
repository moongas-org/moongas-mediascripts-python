# Configure an OpenAI-compatible provider. LLM_* variables take precedence;
# the GITHUB_* variables remain supported for existing GitHub Models setups.
import os
import logging

logger = logging.getLogger(__name__)

from openai import OpenAI

BASE_URL = os.environ.get(
    "LLM_BASE_URL",
    os.environ.get("GITHUB_MODELS_BASE_URL", "https://models.github.ai/inference"),
)
MODEL_NAME = os.environ.get(
    "LLM_MODEL", os.environ.get("GITHUB_MODEL", "openai/gpt-4o-mini")
)


def get_client() -> OpenAI:
    api_key = os.environ.get(
        "LLM_API_KEY", os.environ.get("GITHUB_TOKEN", os.environ.get("OPENAI_API_KEY"))
    )
    if not api_key:
        logger.warning("GITHUB_TOKEN is not set; the API request will likely fail")
    client = OpenAI(base_url=BASE_URL, api_key=api_key)
    return client
