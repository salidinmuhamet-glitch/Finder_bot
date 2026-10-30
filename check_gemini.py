"""Check Gemini credentials and structured generation without using CRM data."""
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"), override=True)

from gemini_reviewer import GeminiReviewer, retry_delay_seconds


def main():
    reviewer = GeminiReviewer(timeout=15)
    if not reviewer.enabled:
        raise SystemExit("GEMINI_API_KEY is missing from the local .env")
    try:
        result = reviewer.analyze_batch(
            [{"company": "Пример организации", "description": "Капитальный ремонт инженерных сетей здания", "region": "Казахстан"}],
            {"business_focus": ["ремонт", "строительство"], "keyword_weights": {}, "regions": [], "customer_types": []},
        )
    except Exception as exc:
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        detail = f"HTTP {status}" if status else type(exc).__name__
        delay = retry_delay_seconds(response) if response is not None else None
        if status == 429 and delay is not None:
            detail += f" (quota reset/retry after about {int(delay)} seconds)"
        raise SystemExit(f"Gemini structured generation failed: {detail}") from None
    if 0 not in result or not isinstance(result[0].get("relevant"), bool):
        raise SystemExit("Gemini returned an invalid structured response")
    print("Gemini API key and structured generation: OK")


if __name__ == "__main__":
    main()
