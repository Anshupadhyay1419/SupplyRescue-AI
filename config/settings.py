"""
Central configuration for SupplyRescue AI.

All tunable values live here so that the rest of the code never reads
environment variables directly. Values come from a `.env` file (optional)
and fall back to safe defaults that work on a normal Windows laptop.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = the folder that contains app.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Load .env if it exists. Real environment variables take priority.
load_dotenv(PROJECT_ROOT / ".env", override=False)


def _read_int(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to a default."""
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _read_float(name: str, default: float) -> float:
    """Read a float environment variable, falling back to a default."""
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return default


# ----- Application -----
APP_NAME = "SupplyRescue AI"
APP_SUBTITLE = "Intelligent Procurement & Supply Chain Recovery"
COMPANY_NAME = "TechNova Electronics"
CURRENCY_SYMBOL = "₹"

# ----- Ollama / LLM -----
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b-instruct-2507-q4_K_M")
OLLAMA_BACKUP_MODEL = os.getenv("OLLAMA_BACKUP_MODEL", "qwen3:4b")
OLLAMA_TEMPERATURE = _read_float("OLLAMA_TEMPERATURE", 0.0)
OLLAMA_NUM_CTX = _read_int("OLLAMA_NUM_CTX", 8192)
OLLAMA_REQUEST_TIMEOUT_SECONDS = _read_int("OLLAMA_REQUEST_TIMEOUT_SECONDS", 120)

# ----- Agent safety limits -----
# Maximum number of LLM -> tool rounds before the agent is forced to stop.
AGENT_MAX_ITERATIONS = _read_int("AGENT_MAX_ITERATIONS", 10)
# Maximum seconds a single tool call may run.
TOOL_TIMEOUT_SECONDS = _read_int("TOOL_TIMEOUT_SECONDS", 20)

# ----- Business rules -----
# Suppliers below this reliability score (0..1) are never selected automatically.
MIN_SUPPLIER_RELIABILITY = _read_float("MIN_SUPPLIER_RELIABILITY", 0.80)

# ----- Storage -----
DATABASE_PATH = PROJECT_ROOT / os.getenv("DATABASE_PATH", "data/supplyrescue.db")
LOG_DIR = PROJECT_ROOT / "logs"
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
