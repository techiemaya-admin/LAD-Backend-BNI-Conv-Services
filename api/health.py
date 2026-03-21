"""Health check and diagnostic endpoints."""
import asyncio
import logging
import os

from db.connection import ClientDBConnection, CoreDBConnection

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    client_db_ok = False
    agent_ok = False

    try:
        async with ClientDBConnection() as conn:
            await conn.fetchval("SELECT 1")
            client_db_ok = True
    except Exception as e:
        logger.error(f"Client DB health check failed: {e}")

    try:
        async with CoreDBConnection() as conn:
            await conn.fetchval("SELECT 1")
            agent_ok = True
    except Exception as e:
        logger.error(f"Agent DB health check failed: {e}")

    return {
        "status": "ok" if (client_db_ok and agent_ok) else "degraded",
        "service": "whatsapp-agent-service",
        "databases": {
            "client_db": "connected" if client_db_ok else "disconnected",
            "salesmaya_agent": "connected" if agent_ok else "disconnected",
        },
    }


@router.get("/test-gemini")
async def test_gemini():
    """Diagnostic endpoint to test Gemini API key and connectivity."""
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return {"status": "error", "detail": "GOOGLE_API_KEY env var is empty"}

    key_preview = f"{api_key[:10]}...{api_key[-4:]}"

    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key)

        model = genai.GenerativeModel(
            "gemini-2.5-flash",
            generation_config={"temperature": 0.1},
        )

        # Simple test call
        response = await asyncio.to_thread(
            model.generate_content, "Say hello in one word."
        )
        result = response.text

        return {
            "status": "ok",
            "key_preview": key_preview,
            "model": "gemini-2.0-flash",
            "response": result,
        }

    except Exception as e:
        return {
            "status": "error",
            "key_preview": key_preview,
            "error_type": type(e).__name__,
            "detail": str(e),
        }
