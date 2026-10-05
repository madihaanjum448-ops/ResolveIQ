import os

# A developer's real .env (with a real key) must never reach unit tests: engine/llm.py skips .env when this is set.
os.environ.setdefault("RESOLVEIQ_NO_DOTENV", "1")
