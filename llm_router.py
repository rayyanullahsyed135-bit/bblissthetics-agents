import os
import requests
from google import genai

LOCAL_LLM_URL = os.environ.get("LOCAL_LLM_URL", "http://localhost:11434/v1/chat/completions").strip()
LOCAL_MODEL = os.environ.get("LOCAL_MODEL", "llama3.1:8b").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()

ai_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None


def generate_ai_text(prompt: str, model_override: str = None, fallback_model: str = "gemini-3.8-flash") -> str:
    target_local_model = model_override or LOCAL_MODEL

    # 1. Try Local RTX 4060 Ollama first ($0, unlimited daily quota)
    if LOCAL_LLM_URL:
        try:
            resp = requests.post(
                LOCAL_LLM_URL,
                json={
                    "model": target_local_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.7
                },
                timeout=30
            )
            if resp.ok:
                return resp.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            print(f"    Local LLM unreachable ({e}), falling back to Gemini...")

    # 2. Automatic Cloud Fallback if laptop is off/asleep
    if ai_client:
        res = ai_client.models.generate_content(model=fallback_model, contents=prompt)
        return res.text.strip()

    raise RuntimeError("Both Local Ollama and Gemini fallback failed.")