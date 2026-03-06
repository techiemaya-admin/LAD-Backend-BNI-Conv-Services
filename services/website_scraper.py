"""
Website scraper — extracts business info (clients, services, about) from a member's website.

Used during ICP Discovery Q1 to enrich the conversation with real data.
"""
import asyncio
import logging
import os
import re

import google.generativeai as genai
import httpx

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 15  # seconds
_MAX_CONTENT_LENGTH = 30_000  # chars to send to LLM


async def scrape_website_for_clients(url: str) -> dict:
    """Fetch a website and extract client/business information using Gemini.

    Returns dict with keys:
      - clients: list of client/company names found
      - services: list of services offered
      - about: short business summary
      - raw_url: the URL that was scraped
    """
    result = {"clients": [], "services": [], "about": "", "raw_url": url}

    # Normalize URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Fetch the page
    try:
        page_text = await _fetch_page_text(url)
        if not page_text:
            logger.warning(f"No content extracted from {url}")
            return result
    except Exception as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        return result

    # Use Gemini to extract structured info
    try:
        extracted = await _extract_with_llm(page_text, url)
        result.update(extracted)
    except Exception as e:
        logger.warning(f"LLM extraction failed for {url}: {e}")

    return result


async def _fetch_page_text(url: str) -> str:
    """Fetch URL and return cleaned text content."""
    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=_FETCH_TIMEOUT,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BNIBot/1.0)"},
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text

    # Quick text extraction — strip tags, scripts, styles
    import re
    # Remove script and style blocks
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # Remove HTML tags
    text = re.sub(r'<[^>]+>', ' ', html)
    # Collapse whitespace
    text = re.sub(r'\s+', ' ', text).strip()

    # Truncate to keep within LLM limits
    return text[:_MAX_CONTENT_LENGTH]


async def _extract_with_llm(page_text: str, url: str) -> dict:
    """Use Gemini to extract clients, services, and about from page text."""
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        logger.warning("GOOGLE_API_KEY not set — skipping LLM extraction")
        return {}

    genai.configure(api_key=api_key)

    prompt = f"""Analyze this website content and extract the following information. Be thorough — look for client logos, testimonials, case studies, portfolio items, partner mentions, and any named companies.

Website: {url}

Content:
{page_text}

Extract and return ONLY valid JSON (no markdown, no explanation):
{{
  "clients": ["list of client/company names mentioned — from testimonials, case studies, logos, portfolio, partnerships, 'trusted by' sections, etc. Include ALL company names found."],
  "services": ["list of services or products the business offers"],
  "about": "1-2 sentence summary of what this business does"
}}

If no clients are found, return an empty list for clients. Always return valid JSON."""

    def _call_sync():
        model = genai.GenerativeModel(
            "gemini-2.5-flash",
            generation_config={
                "temperature": 0.1,
                "response_mime_type": "application/json",
            },
        )
        response = model.generate_content(prompt)
        return response.text

    response_text = await asyncio.to_thread(_call_sync)

    import json
    try:
        data = json.loads(response_text)
        return {
            "clients": data.get("clients", []),
            "services": data.get("services", []),
            "about": data.get("about", ""),
        }
    except Exception as e:
        logger.warning(f"Failed to parse LLM extraction response: {e}")
        return {}
