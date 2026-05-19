import json
import os
import logging
from google import genai
from typing import Dict, Any

logger = logging.getLogger(__name__)

class SelfHealingJSONParser:
    """
    Advanced Self-Healing JSON Engine.
    1. Attempts to strip markdown.
    2. If JSON is fundamentally broken, uses Gemini 2.5 Pro to repair the JSON automatically.
    """
    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
        else:
            self.client = None

    def clean_markdown(self, raw_str: str) -> str:
        if not isinstance(raw_str, str):
            return ""
        cleaned = raw_str.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        return cleaned.strip()

    def parse(self, raw_output: str, fallback_schema_hints: str = "") -> Dict[str, Any]:
        """
        Attempts to parse JSON. If it fails, uses the LLM to self-heal the JSON.
        """
        if isinstance(raw_output, dict):
            return raw_output

        # Step 1: Basic cleanup
        cleaned = self.clean_markdown(raw_output)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            logger.warning(f"Initial JSON parsing failed: {e}. Triggering Self-Healing Engine...")

        # Step 2: Advanced Self-Healing via LLM
        if not self.client:
            logger.error("JSON parsing failed and no API key available for self-healing.")
            raise ValueError("Could not self-heal JSON because no API key is present.")

        prompt = f"""
You are an expert JSON repair engine. 
The following text was supposed to be a valid JSON object but it contains formatting errors.
Fix all syntax errors, ensure keys are quoted, and remove any trailing commas or conversational text.
Return ONLY valid JSON. No markdown blocks, no explanation.
Schema hints (optional): {fallback_schema_hints}

[BROKEN JSON START]
{raw_output}
[BROKEN JSON END]
"""
        try:
            response = self.client.models.generate_content(
                model='gemini-2.5-pro',
                contents=prompt,
            )
            healed_text = self.clean_markdown(response.text)
            
            # Step 3: Parse healed JSON
            healed_json = json.loads(healed_text)
            logger.info("Self-healing successful! Invalid data converted to valid JSON.")
            return healed_json
            
        except Exception as final_e:
            logger.error(f"Self-healing completely failed. Fatal Error: {final_e}")
            raise ValueError(f"Could not self-heal JSON: {final_e}")

if __name__ == "__main__":
    # Test the healing engine
    logging.basicConfig(level=logging.INFO)
    broken_data = "{ 'finish_tag': 'PL-1', manufacturer: 'Unknown',, missing_quotes: true }"
    
    parser = SelfHealingJSONParser()
    try:
        fixed_data = parser.parse(broken_data)
        print(f"Fixed: {fixed_data}")
    except Exception as e:
        print(f"Failed: {e}")
