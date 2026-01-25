"""LLM Utilities"""

import os
import logging

logger = logging.getLogger(__name__)

# Public Endpoint of Ksyun Model Service
PUBLIC_ENDPOINT = "https://kspmas.ksyun.com/v1/"

# Internal Endpoint (Auto-injected by Platform)
# Note: In most cases, we don't need to hardcode this because the platform injects it.
# But for the purpose of "smart switching based on public URL", we might need to know what to switch TO, 
# or simply rely on leaving it empty so the platform default takes over?
# 
# According to user: "OPENAI_BASE_URL 不填会自动选择的" (If empty, it auto-selects).
# So our strategy: If Public URL + Internal Env -> Unset OPENAI_BASE_URL (letting it fall back to platform default).

def get_smart_openai_env() -> None:
    """Smartly configure OpenAI Environment Variables.
    
    If it detects that the code is running in a Ksyun Internal Environment (Serverless/KCE)
    AND the OPENAI_BASE_URL is set to the Public Endpoint, it will unset OPENAI_BASE_URL
    to allow the platform's automatic internal endpoint injection to take effect.
    
    This prevents network hangs caused by accessing public endpoints from internal-only environments.
    """
    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    
    # logic 1: Check if it is the Public Endpoint
    # Normalize by removing trailing slash for comparison
    normalized_url = base_url.rstrip("/")
    normalized_public = PUBLIC_ENDPOINT.rstrip("/")
    
    if normalized_url != normalized_public:
        return

    # Logic 2: Check if running in Internal Environment
    # reliable indicators:
    # - KSYUN_REGION (Injected by Serverless Platform)
    # - K_SERVICE (Injected by Knative/Serverless)
    # - KUBERNETES_SERVICE_HOST (Injected by K8s)
    is_internal = (
        "KSYUN_REGION" in os.environ 
        or "K_SERVICE" in os.environ
        or "KUBERNETES_SERVICE_HOST" in os.environ
    )
    
    if is_internal:
        logger.warning(
            f"Detected Public OPENAI_BASE_URL ({base_url}) in Internal Environment. "
            "Switching to automatic internal endpoint to avoid network timeout."
        )
        # Unset the env var so the underlying library/platform default is used
        # Or explicitly set it to internal generic address if known?
        # User said: "OPENAI_BASE_URL 不填会自动选择". So we unset it.
        del os.environ["OPENAI_BASE_URL"]
        
        # Verify if successful removal
        if "OPENAI_BASE_URL" not in os.environ:
             logger.info("Successfully switched to Internal Endpoint mode.")
