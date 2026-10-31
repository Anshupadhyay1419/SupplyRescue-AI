"""
Agent activity logging.

Every node writes what it ACTUALLY did into `agent_activity_logs` at the
moment it happens. The AI Agent Activity page only reads this table, so the
timeline can never show a step that did not run.
"""

from datetime import datetime

from database.connection import fetch_one, transaction
from utils.logger import get_logger

logger = get_logger(__name__)

# Optional live-progress callbacks for the dashboard: {run_id: function(title)}
_progress_callbacks = {}


def register_progress_callback(run_id: int, callback) -> None:
    if callback:
        _progress_callbacks[run_id] = callback


def unregister_progress_callback(run_id: int) -> None:
    _progress_callbacks.pop(run_id, None)


def _shorten(text, limit: int = 600) -> str | None:
    if text is None:
        return None
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def log_activity(run_id: int, event_type: str, title: str, status: str = "INFO", tool_name: str | None = None,
                 input_summary: str | None = None, result_summary: str | None = None,
                 explanation: str | None = None) -> None:
    """Append one step to the run's timeline (and notify the dashboard if it is watching)."""
    with transaction() as conn:
        next_step = conn.execute(
            "SELECT COALESCE(MAX(step_number), 0) + 1 FROM agent_activity_logs WHERE run_id = ?", (run_id,)
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO agent_activity_logs (run_id, step_number, event_type, title, tool_name, input_summary, "
            "result_summary, status, explanation) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_id, next_step, event_type, title, tool_name, _shorten(input_summary),
             _shorten(result_summary, 1200), status, _shorten(explanation, 1500)),
        )
    logger.info("Run %s step %s: %s", run_id, next_step, title)

    callback = _progress_callbacks.get(run_id)
    if callback:
        try:
            callback(f"{next_step}. {title}" + (f" - {result_summary}" if result_summary else ""))
        except Exception:  # a broken UI callback must never break the agent
            logger.exception("Progress callback failed")


def create_agent_run(run_type: str, model: str, request_id: int | None = None,
                     disruption_id: int | None = None) -> int:
    with transaction() as conn:
        return conn.execute(
            "INSERT INTO agent_runs (run_type, request_id, disruption_id, model, status) VALUES (?, ?, ?, ?, 'RUNNING')",
            (run_type, request_id, disruption_id, model),
        ).lastrowid


def finish_agent_run(run_id: int, status: str, iterations: int, llm_used: bool, summary: str | None) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE agent_runs SET status = ?, iterations = ?, llm_used = ?, final_summary = ?, finished_at = ? "
            "WHERE id = ?",
            (status, iterations, int(llm_used), _shorten(summary, 2000),
             datetime.now().strftime("%Y-%m-%d %H:%M:%S"), run_id),
        )


def has_running_recovery(disruption_id: int) -> bool:
    row = fetch_one(
        "SELECT COUNT(*) AS n FROM agent_runs WHERE disruption_id = ? AND status = 'RUNNING'", (disruption_id,)
    )
    return bool(row and row["n"])
