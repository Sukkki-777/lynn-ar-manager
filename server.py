from __future__ import annotations

import base64
import cgi
import csv
import datetime as dt
import hmac
import hashlib
import io
import json
import mimetypes
import os
import re
import socket
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error, parse, request

import pandas as pd
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.platypus.flowables import Flowable


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
GENERATED_DIR = ROOT / "generated"
STATE_PATH = DATA_DIR / "state.json"


class BikeLogo(Flowable):
    def __init__(self, width: float = 1.05 * inch, height: float = 0.45 * inch):
        super().__init__()
        self.width = width
        self.height = height

    def draw(self) -> None:
        c = self.canv
        c.saveState()
        c.setStrokeColor(colors.HexColor("#0f8a5f"))
        c.setLineWidth(2.3)
        left_x = 0.18 * inch
        right_x = 0.78 * inch
        wheel_y = 0.14 * inch
        radius = 0.12 * inch
        c.circle(left_x, wheel_y, radius, stroke=1, fill=0)
        c.circle(right_x, wheel_y, radius, stroke=1, fill=0)
        c.line(left_x, wheel_y, 0.43 * inch, 0.33 * inch)
        c.line(0.43 * inch, 0.33 * inch, right_x, wheel_y)
        c.line(left_x, wheel_y, 0.54 * inch, wheel_y)
        c.line(0.54 * inch, wheel_y, 0.43 * inch, 0.33 * inch)
        c.line(0.43 * inch, 0.33 * inch, 0.55 * inch, 0.39 * inch)
        c.line(0.68 * inch, 0.37 * inch, 0.84 * inch, 0.37 * inch)
        c.line(0.68 * inch, 0.37 * inch, right_x, wheel_y)
        c.restoreState()


def load_env() -> None:
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for raw in env_path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def today() -> dt.date:
    return dt.date.today()


def default_state() -> dict[str, Any]:
    return {
        "po_pipeline": [],
        "invoices": [],
        "drafts": [],
        "command_results": [],
        "scheduler": default_scheduler(),
        "paused_clients": [],
        "client_policies": {},
        "activity": [
            {
                "id": str(uuid.uuid4()),
                "ts": now_iso(),
                "level": "info",
                "source": "Lynn",
                "kind": "System",
                "reasoning": "Waiting for PO data before making accounts receivable decisions.",
                "outcome": "Ready to read the PO tracker, open invoices, and prepare items for your OK.",
                "message": "Lynn is ready for today's AR work.",
            }
        ],
    }


def default_scheduler() -> dict[str, Any]:
    return {
        "enabled": True,
        "time": "09:00",
        "timezone": "Asia/Shanghai",
        "mode": "Vercel Cron ready",
        "last_run_at": "",
        "next_run_at": next_scheduled_run_iso("09:00"),
    }


def read_state() -> dict[str, Any]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_PATH.exists():
        state = default_state()
        if os.environ.get("SEED_DEMO_DATA", "1").lower() not in {"0", "false", "no"}:
            state["po_pipeline"] = demo_pos()
            summary = po_parse_summary(state["po_pipeline"])
            approval_count = create_invoice_approvals_for_ready_pos(state, source="PO Parser", log_activity=False)
            add_activity(
                state,
                f"Found {summary['total']} demo POs. {summary['ready']} are ready to invoice based on shipment date.",
                source="PO Parser",
                kind="File Reasoning",
                reasoning=(
                    f"The demo state was seeded automatically. {summary['missing_email']} POs are missing client email; "
                    f"{summary['future_shipment']} have future or unknown shipment dates."
                ),
                outcome=f"{approval_count} ready PO approval item{'' if approval_count == 1 else 's'} added to Waiting for your OK.",
            )
        write_state(state)
    state = json.loads(STATE_PATH.read_text())
    state.setdefault("po_pipeline", [])
    state.setdefault("invoices", [])
    state.setdefault("drafts", [])
    state.setdefault("command_results", [])
    state.setdefault("activity", [])
    state.setdefault("paused_clients", [])
    state.setdefault("client_policies", {})
    state.setdefault("scheduler", default_scheduler())
    state["scheduler"].setdefault("enabled", True)
    state["scheduler"].setdefault("time", "09:00")
    state["scheduler"].setdefault("timezone", "Asia/Shanghai")
    state["scheduler"].setdefault("mode", "Vercel Cron ready")
    state["scheduler"].setdefault("last_run_at", "")
    state["scheduler"]["next_run_at"] = next_scheduled_run_iso(state["scheduler"].get("time", "09:00"))
    state["activity"] = [normalize_activity_item(item) for item in state.get("activity", [])]
    state["drafts"] = normalize_drafts(state)
    state["command_results"] = normalize_command_results(state)
    return state


def write_state(state: dict[str, Any]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False))


def next_scheduled_run_iso(time_text: str = "09:00") -> str:
    try:
        hour, minute = [int(part) for part in time_text.split(":", 1)]
    except Exception:
        hour, minute = 9, 0
    tz = dt.timezone(dt.timedelta(hours=8), name="Asia/Shanghai")
    local_now = dt.datetime.now(tz)
    run_at = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if run_at <= local_now:
        run_at = run_at + dt.timedelta(days=1)
    return run_at.isoformat()


def held_until_for_state(state: dict[str, Any]) -> str:
    scheduler = state.get("scheduler") or {}
    return next_scheduled_run_iso(scheduler.get("time", "09:00"))


def held_until_is_future(draft: dict[str, Any]) -> bool:
    value = draft.get("held_until")
    if not value:
        return False
    try:
        held_until = dt.datetime.fromisoformat(str(value))
        if held_until.tzinfo is None:
            held_until = held_until.replace(tzinfo=dt.timezone.utc)
        return held_until > dt.datetime.now(held_until.tzinfo)
    except Exception:
        return False


def normalize_activity_item(item: dict[str, Any]) -> dict[str, Any]:
    message = str(item.get("message", ""))
    if item.get("source") and item.get("kind"):
        if item.get("source") == "Agent":
            item["source"] = "Lynn"
        if item.get("source") == "Agent Reasoning":
            item["source"] = "Lynn Reasoning"
        item.setdefault("reasoning", "")
        item.setdefault("outcome", "")
        return item
    source = "Lynn Reasoning"
    kind = "Decision"
    reasoning = item.get("reasoning", "")
    outcome = item.get("outcome", "")
    if message.startswith("User skipped"):
        source = "Human Approval"
        kind = "Email Skipped"
        reasoning = "The human reviewer chose not to send the agent-suggested follow-up."
        outcome = "Draft removed from Waiting for your OK."
    elif message.startswith("User confirmed send"):
        source = "Human Approval"
        kind = "Email Sent"
        reasoning = "The agent drafted the message, but the customer-facing action required human approval."
        outcome = "Draft marked sent."
    elif "Payment check:" in message:
        source = "User Command"
        kind = "Payment Status"
        reasoning = "The user requested payment status; the agent summarized receivables without creating customer-facing action."
        outcome = "Status-only response."
    elif message.startswith("Drafted"):
        source = "Agent Reasoning"
        kind = "Draft Created"
        reasoning = "The invoice met the agent follow-up criteria and still requires human confirmation before sending."
        outcome = "Placed in Waiting for your OK."
    elif "Stripe" in message or "Payment of" in message:
        source = "Stripe Payment"
        kind = "Payment Reasoning"
        reasoning = "The agent matched payment activity to the invoice record."
        outcome = "Payment state updated."
    item["source"] = source
    item["kind"] = kind
    item["reasoning"] = reasoning
    item["outcome"] = outcome
    return item


def normalize_command_results(state: dict[str, Any]) -> list[dict[str, Any]]:
    normalized_results = []
    for result in state.get("command_results", []):
        command = str(result.get("command", ""))
        if result.get("intent") == "safe_status_check" and is_due_window_query(command):
            horizon_days = command_window_days(command)
            payload = due_window_payload(state, horizon_days)
            if payload["items"]:
                names = ", ".join(
                    f"{item['client']} ({item['invoice']}, {money_text(float(item['amount']), item.get('currency', 'USD'))}, due {item['due_date']})"
                    for item in payload["items"]
                )
                result["message"] = f"Due in the next {horizon_days} days: {names}."
            else:
                result["message"] = f"No unpaid invoices are due in the next {horizon_days} days."
            result["intent"] = "invoice_due_query"
            result["details"] = []
            result["payload"] = payload
        normalized_results.append(result)
    return normalized_results


def normalize_drafts(state: dict[str, Any]) -> list[dict[str, Any]]:
    normalized = []
    invoice_by_id = {item.get("id"): item for item in state.get("invoices", []) if item.get("id")}
    for draft in state.get("drafts", []):
        if draft.get("approval_type") == "create_invoice":
            po = next((item for item in state.get("po_pipeline", []) if item.get("id") == draft.get("po_id")), None)
            if po:
                draft.setdefault("invoice_number", po.get("proposed_invoice_number") or "")
                draft.setdefault("po_number", po.get("po_number") or "")
                draft["subject"] = create_invoice_email_subject(po)
                invoice = invoice_by_id.get(draft.get("invoice_id"))
                existing_link = ""
                if invoice and invoice.get("stripe_payment_link"):
                    existing_link = invoice.get("stripe_payment_link", "")
                elif str(draft.get("payment_link", "")).startswith("http"):
                    existing_link = draft.get("payment_link", "")
                base_body = draft.get("body") if draft.get("edited_at") else create_invoice_email_body(po)
                if existing_link:
                    link_invoice = invoice or invoice_from_po(po)
                    link_invoice["stripe_payment_link"] = existing_link
                    draft["payment_link"] = existing_link
                    draft["body"] = email_with_payment_link(link_invoice, base_body)
                else:
                    draft["payment_link"] = "Stripe payment link pending"
                    draft["body"] = create_invoice_email_body(po)
                draft.update(proposed_invoice_attachment_for_po(po))
        normalized.append(draft)
    return normalized


def add_activity(
    state: dict[str, Any],
    message: str,
    level: str = "info",
    *,
    source: str = "Lynn",
    kind: str = "Decision",
    reasoning: str = "",
    outcome: str = "",
    provider: str = "",
) -> None:
    state.setdefault("activity", []).insert(
        0,
        {
            "id": str(uuid.uuid4()),
            "ts": now_iso(),
            "level": level,
            "source": source,
            "kind": kind,
            "reasoning": reasoning,
            "reasoning_summary": concise_reasoning(reasoning),
            "outcome": outcome,
            "provider": provider,
            "message": message,
        },
    )
    state["activity"] = state["activity"][:80]


def add_command_result(
    state: dict[str, Any],
    command: str,
    message: str,
    intent: str,
    details: list[str] | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "id": str(uuid.uuid4()),
        "command": command,
        "intent": intent,
        "message": message,
        "details": details or [],
        "payload": payload or {},
        "ts": now_iso(),
        "dismissed_at": "",
    }
    state.setdefault("command_results", []).insert(0, result)
    state["command_results"] = state["command_results"][:20]
    return result


def days_overdue(invoice: dict[str, Any]) -> int:
    due = parse_date(invoice.get("due_date"))
    if not due:
        return 0
    return max(0, (today() - due).days)


def invoice_priority(invoice: dict[str, Any], purpose: str = "") -> dict[str, str]:
    amount = float(invoice.get("amount") or 0)
    overdue_days = days_overdue(invoice)
    risk = invoice.get("risk_profile") or {}
    score = int(risk.get("score") or 0)
    purpose_text = purpose.lower()
    if (
        overdue_days >= 30
        or score >= 75
        or amount >= 20000
        or "high-risk" in purpose_text
        or "escalation" in purpose_text
        or invoice.get("status") == "high_risk"
    ):
        return {
            "code": "P0",
            "label": "Critical",
            "reason": "Please review because exposure, aging, or risk level is high.",
        }
    if overdue_days > 0 or "overdue" in purpose_text or invoice.get("status") == "due_soon":
        return {
            "code": "P1",
            "label": "Important",
            "reason": "Please review because collection timing or due-date pressure is active.",
        }
    return {
        "code": "P2",
        "label": "Routine",
        "reason": "Standard AR workflow item with low immediate risk.",
    }


def approval_priority(approval_type: str, payload: dict[str, Any] | None = None) -> dict[str, str]:
    payload = payload or {}
    requested = str(payload.get("priority") or "").upper()
    if requested in {"P0", "P1", "P2"}:
        labels = {"P0": "Critical", "P1": "Important", "P2": "Routine"}
        return {
            "code": requested,
            "label": labels[requested],
            "reason": str(payload.get("priority_reason") or "Priority set by Lynn's recommendation."),
        }
    approval_type = approval_type.lower()
    if approval_type in {"writeoff_review", "pause_emails", "policy_change"}:
        return {
            "code": "P0",
            "label": "Critical",
            "reason": "Sensitive AR policy or relationship action needs senior approval.",
        }
    if approval_type in {"discount_offer"}:
        return {
            "code": "P1",
            "label": "Important",
            "reason": "Customer-facing commercial concession requires finance approval.",
        }
    return {
        "code": "P2",
        "label": "Routine",
        "reason": "Standard approval item.",
    }


def concise_reasoning(text: str, max_sentences: int = 2, max_chars: int = 180) -> str:
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    short = " ".join(sentences[:max_sentences]).strip()
    if len(short) <= max_chars:
        return short
    return short[: max_chars - 1].rstrip() + "…"


def origin_label(source: str = "", command: str = "") -> str:
    if command:
        return "Ask Lynn"
    source_text = str(source or "").lower()
    if "daily" in source_text or "scheduled" in source_text or "demo run" in source_text:
        return "Morning check-in"
    if "po" in source_text or "parser" in source_text or "pipeline" in source_text:
        return "PO Intake"
    if "stripe" in source_text:
        return "Stripe"
    if "risk" in source_text:
        return "Risk Monitor"
    if source:
        return source
    return "Lynn"


def parse_llm_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    return json.loads(cleaned)


def llm_provider() -> str:
    requested = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if requested:
        return requested
    if os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY"):
        return "kimi"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    return ""


def llm_api_key() -> str:
    provider = llm_provider()
    if provider == "kimi":
        return os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY", "")
    if provider == "openai":
        return os.environ.get("OPENAI_API_KEY", "")
    return ""


def llm_model() -> str:
    provider = llm_provider()
    if provider == "kimi":
        return os.environ.get("KIMI_MODEL", "kimi-k2.6")
    if provider == "openai":
        return os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    return ""


def llm_configured() -> bool:
    return bool(llm_api_key())


def llm_display_name() -> str:
    provider = llm_provider()
    if provider == "kimi":
        return "Kimi"
    if provider == "openai":
        return "OpenAI"
    return "LLM"


def llm_business_summary(
    command: str,
    task: str,
    computed_data: dict[str, Any],
    fallback_recommendation: str,
    fallback_reasoning: str,
) -> dict[str, Any]:
    result = {
        "recommendation": fallback_recommendation,
        "reasoning": fallback_reasoning,
        "provider": "rules",
        "openai_error": "",
    }
    if not llm_configured():
        return result
    prompt = f"""
You are the finance copilot for HelloBike Manufacturing Co.
Task: {task}
Use ONLY the supplied computed data. Do not invent invoice amounts, payment status, Stripe records, due dates, or risk scores.
Write concise CFO-friendly reasoning and a recommendation.
Return ONLY JSON with keys: recommendation, reasoning.

User command:
{command}

Computed data:
{json.dumps(computed_data, ensure_ascii=False)}
"""
    try:
        parsed = parse_llm_json(openai_response(prompt, temperature=0.2))
        result["recommendation"] = str(parsed.get("recommendation") or fallback_recommendation)
        result["reasoning"] = str(parsed.get("reasoning") or fallback_reasoning)
        result["provider"] = llm_provider()
    except Exception as exc:
        result["openai_error"] = safe_openai_error(exc)
    return result


def config_status() -> dict[str, Any]:
    provider = llm_provider()
    model = llm_model()
    return {
        "llmConfigured": llm_configured(),
        "llmProvider": provider,
        "llmProviderName": llm_display_name(),
        "llmModel": model,
        "openaiConfigured": bool(os.environ.get("OPENAI_API_KEY")),
        "openaiModel": os.environ.get("OPENAI_MODEL", "gpt-5-mini"),
        "kimiConfigured": bool(os.environ.get("KIMI_API_KEY") or os.environ.get("MOONSHOT_API_KEY")),
        "kimiModel": os.environ.get("KIMI_MODEL", "kimi-k2.6"),
        "stripeConfigured": bool(os.environ.get("STRIPE_SECRET_KEY")),
        "stripeWebhookConfigured": bool(os.environ.get("STRIPE_WEBHOOK_SECRET")),
        "exaConfigured": bool(os.environ.get("EXA_API_KEY")),
        "senderEmail": os.environ.get("SENDER_EMAIL", "finance@example.com"),
    }


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def text_response(handler: BaseHTTPRequestHandler, text: str, status: int = 200) -> None:
    raw = text.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


def read_json_body(handler: BaseHTTPRequestHandler) -> Any:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def parse_date(value: Any) -> dt.date | None:
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    parsed = pd.to_datetime(str(value), errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def parse_amount(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = re.sub(r"[^0-9.\-]", "", str(value))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def parse_terms_days(value: Any) -> int:
    text = str(value or "").lower()
    match = re.search(r"(\d+)", text)
    if match:
        return int(match.group(1))
    return 30


def normalize_currency(value: Any) -> str:
    text = str(value or "USD").upper().strip()
    if len(text) >= 3:
        return text[:3]
    return "USD"


def due_date_for(invoice_date: dt.date, terms: str) -> dt.date:
    return invoice_date + dt.timedelta(days=parse_terms_days(terms))


def pre_due_lead_days(terms: str) -> int:
    term_days = parse_terms_days(terms)
    if term_days >= 90:
        return 30
    if term_days >= 60:
        return 14
    return 7


def overdue_followup_purpose(overdue_days: int) -> str:
    if overdue_days >= 30:
        return "collection escalation"
    if overdue_days >= 7:
        return "firm overdue follow-up"
    return "friendly overdue reminder"


def status_for_due(due_date: dt.date, paid: bool = False, risk_score: int = 0) -> str:
    if paid:
        return "paid"
    if risk_score >= 75:
        return "high_risk"
    days = (due_date - today()).days
    if days < 0:
        return "high_risk" if abs(days) >= 30 else "overdue"
    if days <= 7:
        return "due_soon"
    return "open"


def heuristic_mapping(columns: list[str]) -> dict[str, str | None]:
    patterns = {
        "po_number": ["po number", "po#", "po", "purchase order", "order id", "order"],
        "client_name": ["client", "customer", "buyer", "company", "account"],
        "client_email": ["email", "contact email", "recipient"],
        "amount": ["amount", "total", "value", "price", "usd"],
        "currency": ["currency", "ccy"],
        "payment_terms": ["payment terms", "terms", "net"],
        "po_date": ["po date", "date", "order date", "invoice date"],
        "shipment_date": ["shipment", "ship date", "delivery"],
        "notes": ["notes", "description", "memo", "item"],
    }
    lower = {c.lower().strip(): c for c in columns}
    mapping: dict[str, str | None] = {}
    for key, aliases in patterns.items():
        found = None
        for alias in aliases:
            for low, original in lower.items():
                if alias in low:
                    found = original
                    break
            if found:
                break
        mapping[key] = found
    return mapping


def extract_output_text(response_payload: dict[str, Any]) -> str:
    if isinstance(response_payload.get("output_text"), str):
        return response_payload["output_text"]
    chunks: list[str] = []
    for item in response_payload.get("output", []):
        for content in item.get("content", []):
            text = content.get("text") or content.get("output_text")
            if isinstance(text, str):
                chunks.append(text)
    return "\n".join(chunks).strip()


def extract_chat_completion_text(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        chunks = []
        for item in content:
            text = item.get("text") if isinstance(item, dict) else None
            if isinstance(text, str):
                chunks.append(text)
        return "\n".join(chunks).strip()
    return ""


def safe_openai_error(exc: Exception) -> str:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return f"{llm_display_name()} request timed out; using deterministic fallback."
    if isinstance(exc, RuntimeError):
        text = str(exc)
        if text.startswith(("OpenAI ", "Kimi ", "LLM ")):
            return text
        return f"{llm_display_name()} request failed; using deterministic fallback."
    if isinstance(exc, error.HTTPError):
        if exc.code == 401:
            return f"{llm_display_name()} authentication failed. Check the key in .env."
        return f"{llm_display_name()} API returned HTTP {exc.code}."
    if isinstance(exc, error.URLError):
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return f"{llm_display_name()} request timed out; using deterministic fallback."
        return f"{llm_display_name()} network request failed from this server."
    text = str(exc)
    if "API_KEY" in text:
        return f"{llm_display_name()} configuration failed. Check .env."
    return f"{llm_display_name()} request failed; using deterministic fallback."


def openai_response(prompt: str, *, temperature: float | None = None) -> str:
    provider = llm_provider()
    api_key = llm_api_key()
    if not api_key:
        raise RuntimeError(f"{llm_display_name()} API key is not configured")
    model = llm_model()
    if provider == "kimi":
        endpoint = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1").rstrip("/") + "/chat/completions"
        body = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": int(os.environ.get("KIMI_MAX_TOKENS", "1000")),
            "thinking": {"type": os.environ.get("KIMI_THINKING", "disabled")},
        }
        if os.environ.get("KIMI_TEMPERATURE"):
            body["temperature"] = float(os.environ["KIMI_TEMPERATURE"])
    else:
        endpoint = "https://api.openai.com/v1/responses"
        body = {
            "model": model,
            "input": prompt,
        }
        if temperature is not None or os.environ.get("OPENAI_TEMPERATURE"):
            body["temperature"] = float(os.environ.get("OPENAI_TEMPERATURE", temperature if temperature is not None else 0.2))
    raw = json.dumps(body).encode("utf-8")
    req = request.Request(
        endpoint,
        data=raw,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=float(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        raise RuntimeError(safe_openai_error(exc)) from exc
    except error.URLError as exc:
        raise RuntimeError(safe_openai_error(exc)) from exc
    except TimeoutError as exc:
        raise RuntimeError(safe_openai_error(exc)) from exc
    text = extract_chat_completion_text(payload) if provider == "kimi" else extract_output_text(payload)
    if not text:
        raise RuntimeError(f"{llm_display_name()} response did not contain text")
    return text


def ai_column_mapping(columns: list[str], sample_rows: list[dict[str, Any]]) -> dict[str, str | None]:
    prompt = f"""
You are mapping a purchase-order tracker into accounts receivable fields.
Return ONLY valid JSON. Each value must be one of the input column names or null.

Required JSON keys:
po_number, client_name, client_email, amount, currency, payment_terms, po_date, shipment_date, notes

Columns:
{json.dumps(columns, ensure_ascii=False)}

Sample rows:
{json.dumps(sample_rows, ensure_ascii=False, default=str)}
"""
    text = openai_response(prompt)
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    parsed = json.loads(cleaned)
    allowed = set(columns)
    return {key: (value if value in allowed else None) for key, value in parsed.items()}


def load_dataframe(filename: str, content: bytes) -> pd.DataFrame:
    suffix = Path(filename).suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(io.BytesIO(content))
    if suffix == ".csv":
        return pd.read_csv(io.BytesIO(content))
    try:
        return pd.read_excel(io.BytesIO(content))
    except Exception:
        return pd.read_csv(io.BytesIO(content))


def build_invoice(row: dict[str, Any], mapping: dict[str, str | None], index: int) -> dict[str, Any]:
    def pick(field: str, default: Any = "") -> Any:
        column = mapping.get(field)
        if column and column in row and not pd.isna(row[column]):
            return row[column]
        return default

    invoice_date = parse_date(pick("po_date")) or today()
    terms = str(pick("payment_terms", "Net 30") or "Net 30")
    due = due_date_for(invoice_date, terms)
    amount = parse_amount(pick("amount"))
    po_number = str(pick("po_number", f"PO-{today().year}-{index + 1:03d}"))
    client = str(pick("client_name", "Unknown Client") or "Unknown Client")
    invoice_id = f"INV-{today().year}-{index + 1:03d}"
    return {
        "id": str(uuid.uuid4()),
        "invoice_number": invoice_id,
        "po_number": po_number,
        "client_name": client,
        "client_email": str(pick("client_email", "")),
        "amount": round(amount, 2),
        "currency": normalize_currency(pick("currency", "USD")),
        "payment_terms": terms,
        "invoice_date": invoice_date.isoformat(),
        "due_date": due.isoformat(),
        "shipment_date": (parse_date(pick("shipment_date")) or invoice_date).isoformat(),
        "notes": str(pick("notes", "")),
        "status": status_for_due(due),
        "stripe_payment_link": "",
        "stripe_payment_link_id": "",
        "pdf_path": "",
        "risk_profile": None,
        "payment_details": None,
        "created_at": now_iso(),
        "paid_at": "",
    }


SHIPMENT_INVOICE_WINDOW_DAYS = 10


def build_po_record(row: dict[str, Any], mapping: dict[str, str | None], index: int) -> dict[str, Any]:
    invoice = build_invoice(row, mapping, index)
    shipment = parse_date(invoice.get("shipment_date"))
    missing = []
    if not invoice.get("client_email"):
        missing.append("client email")
    if float(invoice.get("amount", 0)) <= 0:
        missing.append("amount")
    if not shipment:
        missing.append("shipment date")
    if missing:
        po_status = "needs_review"
        reason = f"Missing {', '.join(missing)}."
    elif shipment and shipment > today() + dt.timedelta(days=SHIPMENT_INVOICE_WINDOW_DAYS):
        po_status = "upcoming"
        reason = f"Shipment date is {(shipment - today()).days} days away; wait until {SHIPMENT_INVOICE_WINDOW_DAYS}-day invoice window."
    else:
        po_status = "ready_to_invoice"
        reason = "Shipment date is complete or within 10 days; Lynn can prepare the invoice for your OK."
    return {
        "id": invoice["id"],
        "po_number": invoice["po_number"],
        "proposed_invoice_number": invoice["invoice_number"],
        "client_name": invoice["client_name"],
        "client_email": invoice["client_email"],
        "amount": invoice["amount"],
        "currency": invoice["currency"],
        "payment_terms": invoice["payment_terms"],
        "po_date": invoice["invoice_date"],
        "shipment_date": invoice["shipment_date"],
        "notes": invoice["notes"],
        "status": po_status,
        "status_reason": reason,
        "created_at": invoice["created_at"],
        "approved_at": "",
        "invoice_id": "",
    }


def po_duplicate_key(po: dict[str, Any]) -> str:
    po_number = str(po.get("po_number") or "").strip().lower()
    if po_number:
        return f"po:{po_number}"
    client = str(po.get("client_name") or "").strip().lower()
    amount = round(float(po.get("amount", 0) or 0), 2)
    shipment = str(po.get("shipment_date") or "").strip()
    terms = str(po.get("payment_terms") or "").strip().lower()
    return f"fallback:{client}|{amount:.2f}|{shipment}|{terms}"


def existing_po_keys(state: dict[str, Any]) -> set[str]:
    keys = set()
    for po in state.get("po_pipeline", []):
        keys.add(po_duplicate_key(po))
    for invoice in state.get("invoices", []):
        keys.add(po_duplicate_key(invoice))
    return keys


def merge_uploaded_pos(state: dict[str, Any], uploaded_pos: list[dict[str, Any]]) -> dict[str, Any]:
    seen = existing_po_keys(state)
    added: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    for po in uploaded_pos:
        key = po_duplicate_key(po)
        if key in seen:
            duplicates.append(po)
            continue
        seen.add(key)
        added.append(po)
    state.setdefault("po_pipeline", []).extend(added)
    return {
        "added": added,
        "duplicates": duplicates,
        "added_count": len(added),
        "duplicate_count": len(duplicates),
        "uploaded_count": len(uploaded_pos),
    }


def create_invoice_email_subject(po: dict[str, Any]) -> str:
    invoice_number = po.get("proposed_invoice_number") or po.get("po_number", "Invoice")
    return f"Invoice {invoice_number} for {po.get('po_number', 'your order')}"


def create_invoice_email_body(po: dict[str, Any]) -> str:
    shipment_date = parse_date(po.get("shipment_date")) or parse_date(po.get("po_date")) or today()
    due = due_date_for(shipment_date, po.get("payment_terms", "Net 30"))
    return (
        f"Hi {po.get('client_name', 'there')},\n\n"
        f"Please find attached invoice {po.get('proposed_invoice_number') or po.get('po_number')} "
        f"for {po.get('po_number')}.\n\n"
        f"Invoice amount: {po.get('currency', 'USD')} {float(po.get('amount', 0)):,.2f}\n"
        f"Payment terms: {po.get('payment_terms', 'Net 30')}\n"
        f"Due date: {due.isoformat()}\n\n"
        "You can pay securely here: Stripe payment link pending\n\n"
        "Please let us know if you need anything else from our side.\n\n"
        "Best,\nFinance Team"
    )


def proposed_invoice_attachment_for_po(po: dict[str, Any]) -> dict[str, str]:
    invoice = invoice_from_po(po)
    pdf_path = generate_invoice_pdf(invoice)
    return {
        "attachment_url": pdf_path,
        "attachment_name": f"{invoice_pdf_file_stem(invoice)}.pdf",
    }


def prepare_create_invoice_package(po: dict[str, Any]) -> dict[str, Any]:
    invoice = invoice_from_po(po)
    pdf_path = generate_invoice_pdf(invoice)
    stripe_error = ""
    if os.environ.get("STRIPE_SECRET_KEY"):
        try:
            create_stripe_link(invoice)
        except Exception as exc:
            stripe_error = safe_openai_error(exc)
    body = email_with_payment_link(invoice, create_invoice_email_body(po))
    return {
        "invoice": invoice,
        "body": body,
        "payment_link": invoice.get("stripe_payment_link") or "Stripe payment link pending",
        "payment_link_id": invoice.get("stripe_payment_link_id", ""),
        "attachment_url": pdf_path,
        "attachment_name": f"{invoice_pdf_file_stem(invoice)}.pdf",
        "stripe_error": stripe_error,
    }


def invoice_from_po(po: dict[str, Any]) -> dict[str, Any]:
    shipment_date = parse_date(po.get("shipment_date")) or today()
    invoice_date = shipment_date
    terms = po.get("payment_terms", "Net 30")
    due = due_date_for(invoice_date, terms)
    return {
        "id": str(uuid.uuid4()),
        "invoice_number": po.get("proposed_invoice_number") or f"INV-{today().year}-{uuid.uuid4().hex[:4].upper()}",
        "po_number": po.get("po_number", ""),
        "client_name": po.get("client_name", "Unknown Client"),
        "client_email": po.get("client_email", ""),
        "amount": round(float(po.get("amount", 0)), 2),
        "currency": po.get("currency", "USD"),
        "payment_terms": terms,
        "invoice_date": invoice_date.isoformat(),
        "due_date": due.isoformat(),
        "shipment_date": shipment_date.isoformat(),
        "notes": po.get("notes", ""),
        "status": status_for_due(due),
        "stripe_payment_link": "",
        "stripe_payment_link_id": "",
        "pdf_path": "",
        "risk_profile": None,
        "payment_details": None,
        "created_at": now_iso(),
        "paid_at": "",
    }


def forecast_record_from_po(po: dict[str, Any]) -> dict[str, Any]:
    shipment_date = parse_date(po.get("shipment_date")) or parse_date(po.get("po_date")) or today()
    terms = po.get("payment_terms", "Net 30")
    due = due_date_for(shipment_date, terms)
    return {
        "id": f"pipeline:{po.get('id', po.get('po_number', ''))}",
        "invoice_number": po.get("proposed_invoice_number") or po.get("po_number", ""),
        "po_number": po.get("po_number", ""),
        "client_name": po.get("client_name", "Unknown Client"),
        "client_email": po.get("client_email", ""),
        "amount": round(float(po.get("amount", 0) or 0), 2),
        "currency": po.get("currency", "USD"),
        "payment_terms": terms,
        "invoice_date": shipment_date.isoformat(),
        "due_date": due.isoformat(),
        "shipment_date": shipment_date.isoformat(),
        "notes": po.get("notes", ""),
        "status": status_for_due(due),
        "risk_profile": None,
        "forecast_source": "po_pipeline",
    }


def forecast_records_from_state(state: dict[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_po_numbers: set[str] = set()
    seen_invoice_ids: set[str] = set()
    for invoice in state.get("invoices", []):
        records.append(invoice)
        if invoice.get("id"):
            seen_invoice_ids.add(str(invoice.get("id")))
        if invoice.get("po_number"):
            seen_po_numbers.add(str(invoice.get("po_number")))
    for po in state.get("po_pipeline", []):
        if po.get("status") in {"invoiced", "blocked"}:
            continue
        if po.get("invoice_id") and str(po.get("invoice_id")) in seen_invoice_ids:
            continue
        po_number = str(po.get("po_number") or "")
        if po_number and po_number in seen_po_numbers:
            continue
        if float(po.get("amount", 0) or 0) <= 0:
            continue
        records.append(forecast_record_from_po(po))
        if po_number:
            seen_po_numbers.add(po_number)
    return records


def auto_create_invoice_from_po(state: dict[str, Any], po: dict[str, Any], source: str = "PO Pipeline") -> dict[str, Any] | None:
    if po.get("invoice_id") or po.get("status") in {"invoiced", "needs_review", "blocked", "upcoming"}:
        return None
    invoice = invoice_from_po(po)
    state.setdefault("invoices", []).append(invoice)
    po["status"] = "invoiced"
    po["approved_at"] = now_iso()
    po["invoice_id"] = invoice["id"]
    add_activity(
        state,
        f"Agent created {invoice['invoice_number']} from {po.get('po_number')}.",
        source=source,
        kind="Autonomous Invoice Creation",
        reasoning=(
            f"{po.get('po_number')} shipment date is {po.get('shipment_date')}. "
            f"It is complete or within the {SHIPMENT_INVOICE_WINDOW_DAYS}-day invoice window, so invoice date is based on shipment date."
        ),
        outcome=(
            f"{invoice['invoice_number']} moved to Active Invoices. "
            "Customer-facing email still requires human approval."
        ),
    )
    return invoice


def create_invoice_approval(
    state: dict[str, Any],
    po: dict[str, Any],
    source: str = "PO Pipeline",
    *,
    log_activity: bool = True,
) -> dict[str, Any]:
    key = f"create_invoice:{po['id']}"
    for draft in state.setdefault("drafts", []):
        if draft.get("dedupe_key") == key and draft.get("status") == "awaiting_confirmation":
            return draft
        if draft.get("dedupe_key") == key and draft.get("status") == "held" and held_until_is_future(draft):
            return draft
    package = prepare_create_invoice_package(po)
    approval = {
        "id": str(uuid.uuid4()),
        "approval_type": "create_invoice",
        "po_id": po["id"],
        "invoice_id": "",
        "invoice_number": po.get("proposed_invoice_number") or "",
        "po_number": po.get("po_number", ""),
        "purpose": "create invoice",
        "dedupe_key": key,
        "priority": "P2",
        "priority_label": "Routine",
        "priority_reason": "Please review before Lynn moves this PO into Active Invoices.",
        "client_name": po.get("client_name", ""),
        "client_email": po.get("client_email", ""),
        "subject": create_invoice_email_subject(po),
        "body": package["body"],
        "payment_link": package["payment_link"],
        "payment_link_id": package["payment_link_id"],
        "source": source,
        "origin_label": origin_label(source),
        "attachment_url": package["attachment_url"],
        "attachment_name": package["attachment_name"],
        "stripe_error": package["stripe_error"],
        "status": "awaiting_confirmation",
        "created_at": now_iso(),
        "sent_at": "",
    }
    state.setdefault("drafts", []).insert(0, approval)
    if log_activity:
        add_activity(
            state,
            f"Create-invoice approval prepared for {po.get('po_number')}.",
            source=source,
            kind="Pending Approval",
            reasoning=po.get("status_reason", "PO is ready for invoice approval."),
            outcome="Waiting for your OK before moving PO into Active Invoices.",
        )
    return approval


def create_invoice_approvals_for_ready_pos(
    state: dict[str, Any],
    source: str = "PO Pipeline",
    *,
    log_activity: bool = True,
) -> int:
    created = 0
    for po in state.get("po_pipeline", []):
        if po.get("status") == "ready_to_invoice" and not po.get("invoice_id"):
            before = len(state.setdefault("drafts", []))
            create_invoice_approval(state, po, source=source, log_activity=log_activity)
            if len(state.get("drafts", [])) > before:
                created += 1
    return created


def backfill_create_invoice_payment_links(state: dict[str, Any], source: str = "Stripe Payment") -> dict[str, Any]:
    updated = 0
    errors: list[dict[str, str]] = []
    for draft in state.get("drafts", []):
        if draft.get("status") != "awaiting_confirmation" or draft.get("approval_type") != "create_invoice":
            continue
        if str(draft.get("payment_link", "")).startswith("http"):
            continue
        po = next((item for item in state.get("po_pipeline", []) if item.get("id") == draft.get("po_id")), None)
        if not po:
            continue
        package = prepare_create_invoice_package(po)
        if not str(package.get("payment_link", "")).startswith("http"):
            errors.append(
                {
                    "invoice_number": draft.get("invoice_number", ""),
                    "po_number": draft.get("po_number", ""),
                    "error": package.get("stripe_error", "Stripe did not return a payment link."),
                }
            )
            continue
        draft["body"] = package["body"]
        draft["payment_link"] = package["payment_link"]
        draft["payment_link_id"] = package["payment_link_id"]
        draft["attachment_url"] = package["attachment_url"]
        draft["attachment_name"] = package["attachment_name"]
        updated += 1
    if updated or errors:
        add_activity(
            state,
            f"Stripe payment links prepared for {updated} waiting invoice draft{'' if updated == 1 else 's'}.",
            "warn" if errors else "info",
            source=source,
            kind="Stripe Tool Use",
            reasoning="Lynn backfilled payment links for invoice drafts already waiting for approval so every customer email has a Stripe checkout URL.",
            outcome=(
                f"{updated} draft{'' if updated == 1 else 's'} updated; "
                f"{len(errors)} error{'' if len(errors) == 1 else 's'}."
            ),
        )
    return {"updated": updated, "errors": errors}


def auto_create_invoices_for_ready_pos(state: dict[str, Any], source: str = "PO Pipeline") -> None:
    for po in state.get("po_pipeline", []):
        if po.get("status") == "ready_to_invoice" and not po.get("invoice_id"):
            auto_create_invoice_from_po(state, po, source=source)


def refresh_po_pipeline(
    state: dict[str, Any],
    source: str = "Scheduled Daily Run",
    *,
    log_activity: bool = True,
) -> int:
    created = 0
    for po in state.get("po_pipeline", []):
        if po.get("invoice_id") or po.get("status") in {"invoiced", "needs_review", "blocked"}:
            continue
        shipment = parse_date(po.get("shipment_date"))
        if shipment and shipment <= today() + dt.timedelta(days=SHIPMENT_INVOICE_WINDOW_DAYS):
            po["status"] = "ready_to_invoice"
            po["status_reason"] = "Shipment date is complete or within 10 days; Lynn can prepare the invoice for your OK."
            before = len(state.setdefault("drafts", []))
            create_invoice_approval(state, po, source=source, log_activity=log_activity)
            if len(state.get("drafts", [])) > before:
                created += 1
        elif shipment:
            po["status"] = "upcoming"
            po["status_reason"] = f"Shipment date is {(shipment - today()).days} days away; wait until 10-day invoice window."
    return created


def invoice_metrics(invoices: list[dict[str, Any]]) -> dict[str, Any]:
    active = [i for i in invoices if i.get("status") != "paid"]
    overdue = [
        i
        for i in active
        if parse_date(i.get("due_date")) and parse_date(i.get("due_date")) < today()
    ]
    due_week = [
        i
        for i in active
        if parse_date(i.get("due_date"))
        and 0 <= (parse_date(i.get("due_date")) - today()).days <= 7
    ]
    paid_month = [
        i
        for i in invoices
        if i.get("status") == "paid"
        and i.get("paid_at")
        and i["paid_at"][:7] == today().isoformat()[:7]
    ]
    return {
        "outstanding": round(sum(float(i.get("amount", 0)) for i in active), 2),
        "overdue": round(sum(float(i.get("amount", 0)) for i in overdue), 2),
        "dueThisWeek": round(sum(float(i.get("amount", 0)) for i in due_week), 2),
        "paidThisMonth": round(sum(float(i.get("amount", 0)) for i in paid_month), 2),
        "activeCount": len(active),
        "overdueCount": len(overdue),
        "highRiskCount": len([i for i in active if i.get("status") == "high_risk"]),
    }


def payment_details_for(invoice: dict[str, Any]) -> dict[str, Any] | None:
    if invoice.get("status") != "paid":
        return None
    if invoice.get("payment_details"):
        return invoice["payment_details"]
    paid_at = invoice.get("paid_at") or now_iso()
    return {
        "provider": "Stripe",
        "amount": round(float(invoice.get("amount", 0)), 2),
        "currency": (invoice.get("currency") or "USD").upper(),
        "method": "card",
        "paid_at": paid_at,
        "reference": f"stripe_record_{invoice.get('invoice_number', 'invoice').lower()}",
        "settlement_status": "succeeded",
    }


def parse_forecast_window(command: str) -> int:
    text = command.lower()
    match = re.search(r"(\d+)\s*(day|days|week|weeks|month|months)", text)
    if not match:
        return 30
    amount = int(match.group(1))
    unit = match.group(2)
    if unit.startswith("week"):
        return amount * 7
    if unit.startswith("month"):
        return amount * 30
    return amount


def is_cash_flow_command(command: str) -> bool:
    text = command.lower()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    if any(
        marker in compact
        for marker in [
            "cashflow",
            "cashforecast",
            "cashinflow",
            "expectedcash",
            "collectionforecast",
            "receivablesforecast",
            "arforecast",
        ]
    ):
        return True
    if any(phrase in text for phrase in ["cash flow", "cash-flow", "inflow forecast", "collection forecast"]):
        return True
    if ("forecast" in text or "inflow" in text) and any(
        marker in compact for marker in ["cash", "collection", "receivable", "ar"]
    ):
        return True
    if "coming" in text and any(marker in compact for marker in ["cash", "cashflow", "inflow"]):
        return True
    has_window = re.search(r"\b\d+\s*(day|days|week|weeks|month|months)\b", text)
    return bool(has_window and any(marker in compact for marker in ["cash", "cashflow", "inflow"]))


def collection_probability(invoice: dict[str, Any]) -> float:
    if invoice.get("status") == "paid":
        return 0.0
    risk = invoice.get("risk_profile") or baseline_risk_profile(invoice)
    score = int(risk.get("score", 0))
    overdue_days = days_overdue(invoice)
    if score >= 75 or overdue_days >= 30:
        return 0.25
    if invoice.get("status") == "overdue" or overdue_days > 0:
        return 0.55
    if invoice.get("status") == "due_soon":
        return 0.85
    return 0.9


def forecast_cash_flow(invoices: list[dict[str, Any]], horizon_days: int) -> dict[str, Any]:
    end_date = today() + dt.timedelta(days=horizon_days)
    eligible = []
    overdue = []
    future = []
    for invoice in invoices:
        if invoice.get("status") == "paid":
            continue
        due = parse_date(invoice.get("due_date"))
        if not due:
            continue
        amount = float(invoice.get("amount", 0))
        probability = collection_probability(invoice)
        record = {
            "invoice": invoice.get("invoice_number", ""),
            "client": invoice.get("client_name", ""),
            "amount": amount,
            "currency": invoice.get("currency", "USD"),
            "due_date": due.isoformat(),
            "status": invoice.get("status", ""),
            "source": invoice.get("forecast_source", "invoice"),
            "probability": probability,
            "risk_adjusted_amount": round(amount * probability, 2),
        }
        if due < today():
            overdue.append(record)
        elif due <= end_date:
            future.append(record)
        if due <= end_date:
            eligible.append(record)

    contractual = round(sum(item["amount"] for item in eligible), 2)
    risk_adjusted = round(sum(item["risk_adjusted_amount"] for item in eligible), 2)
    at_risk = round(sum(item["amount"] for item in overdue), 2)
    return {
        "horizon_days": horizon_days,
        "end_date": end_date.isoformat(),
        "contractual": contractual,
        "risk_adjusted": risk_adjusted,
        "at_risk": at_risk,
        "future": future,
        "overdue": overdue,
    }


def po_parse_summary(invoices: list[dict[str, Any]]) -> dict[str, int]:
    ready = 0
    missing_email = 0
    future_shipment = 0
    for invoice in invoices:
        shipment = parse_date(invoice.get("shipment_date"))
        if not invoice.get("client_email"):
            missing_email += 1
        if shipment and shipment <= today() + dt.timedelta(days=SHIPMENT_INVOICE_WINDOW_DAYS):
            ready += 1
        else:
            future_shipment += 1
    return {
        "total": len(invoices),
        "ready": ready,
        "missing_email": missing_email,
        "future_shipment": future_shipment,
    }


def invoice_decision_reason(invoice: dict[str, Any], purpose: str) -> str:
    overdue = days_overdue(invoice)
    email_count = int(invoice.get("email_sent_count", 0))
    purpose_text = purpose.lower()
    if purpose == "pre-due reminder":
        due = parse_date(invoice.get("due_date"))
        days = (due - today()).days if due else 0
        lead = pre_due_lead_days(invoice.get("payment_terms", "Net 30"))
        return (
            f"{invoice['invoice_number']} is due in {days} days under {invoice.get('payment_terms', 'standard terms')}; "
            f"Lynn uses a {lead}-day pre-due reminder window for this term. "
            "The agent prepares a friendly pre-due reminder so finance can confirm before sending."
        )
    if "partial payment" in purpose_text:
        payment = invoice.get("payment_details") or {}
        remaining = float(payment.get("remaining_amount") or 0)
        return (
            f"Stripe payment for {invoice['invoice_number']} does not fully match the invoice amount; "
            f"{invoice.get('currency', 'USD')} {remaining:,.2f} remains outstanding."
        )
    if purpose_text == "collection escalation":
        return (
            f"{invoice['client_name']} is {overdue} days overdue, crossing the 30-day escalation point. "
            "Lynn prepares a formal collection message and flags that future shipments may need review."
        )
    if purpose_text == "firm overdue follow-up":
        return (
            f"{invoice['client_name']} is {overdue} days overdue, crossing the 7-day follow-up point. "
            "Lynn prepares a firmer message but waits for your OK before sending."
        )
    if "overdue" in purpose_text:
        return (
            f"{invoice['client_name']} is {overdue} day{'s' if overdue != 1 else ''} overdue. "
            "Lynn prepares a friendly overdue reminder and keeps it in Waiting for your OK."
        )
    return (
        f"{invoice['invoice_number']} shipment and payment terms support creating an invoice email now; due date is {invoice.get('due_date')}."
    )


def record_anomaly(
    state: dict[str, Any],
    invoice: dict[str, Any],
    anomaly_type: str,
    *,
    source: str,
    reasoning: str,
    action: str,
    level: str = "warn",
) -> None:
    key = f"{today().isoformat()}:{invoice.get('id')}:{anomaly_type}"
    seen = set(state.setdefault("anomaly_keys", []))
    if key in seen:
        return
    seen.add(key)
    state["anomaly_keys"] = list(seen)[-200:]
    add_activity(
        state,
        f"{anomaly_type}: {invoice.get('invoice_number')} · {invoice.get('client_name')}",
        level,
        source=source,
        kind="Risk Alert",
        reasoning=reasoning,
        outcome=action,
    )


def same_quarter(date_a: dt.date, date_b: dt.date) -> bool:
    return date_a.year == date_b.year and ((date_a.month - 1) // 3) == ((date_b.month - 1) // 3)


def detect_invoice_anomalies(
    state: dict[str, Any],
    invoice: dict[str, Any],
    metrics: dict[str, Any],
    *,
    source: str,
) -> None:
    due = parse_date(invoice.get("due_date"))
    if not due:
        return
    overdue_days = max(0, (today() - due).days)
    client = invoice.get("client_name")
    client_invoices = [item for item in state.get("invoices", []) if item.get("client_name") == client]
    client_overdue_this_quarter = [
        item
        for item in client_invoices
        if parse_date(item.get("due_date"))
        and parse_date(item.get("due_date")) < today()
        and same_quarter(parse_date(item.get("due_date")), today())
    ]
    paid_history = [item for item in client_invoices if item.get("status") == "paid"]
    previous_overdue = [
        item
        for item in client_invoices
        if item.get("id") != invoice.get("id")
        and parse_date(item.get("due_date"))
        and parse_date(item.get("due_date")) < today()
    ]

    payment = invoice.get("payment_details") or {}
    remaining = float(payment.get("remaining_amount") or 0)
    if invoice.get("status") == "partial_paid" or remaining > 0:
        record_anomaly(
            state,
            invoice,
            "Partial payment",
            source=source,
            reasoning=(
                f"Stripe received {payment.get('currency', invoice.get('currency', 'USD'))} "
                f"{float(payment.get('amount', 0)):,.2f}, but invoice amount is {invoice.get('currency', 'USD')} "
                f"{float(invoice.get('amount', 0)):,.2f}."
            ),
            action=f"Generate a balance follow-up for the remaining ${remaining:,.2f} and ask the user to confirm.",
        )

    if overdue_days > 0 and paid_history and not previous_overdue:
        record_anomaly(
            state,
            invoice,
            "Payment behavior change",
            source=source,
            reasoning=f"{client} has paid prior invoices, but {invoice['invoice_number']} is now {overdue_days} days overdue.",
            action="Flag as unusual behavior and increase risk level for this invoice.",
        )

    if overdue_days > 0 and len(client_overdue_this_quarter) >= 2:
        record_anomaly(
            state,
            invoice,
            "Repeated overdue",
            source=source,
            reasoning=f"{client} has {len(client_overdue_this_quarter)} overdue invoices this quarter.",
            action="Escalate tone and recommend reviewing payment terms for future orders.",
        )

    email_count = int(invoice.get("email_sent_count", 0))
    if overdue_days > 0 and email_count >= 3:
        record_anomaly(
            state,
            invoice,
            "Long no-response",
            source=source,
            reasoning=f"{email_count} automated follow-up emails were sent for {invoice['invoice_number']} with no payment recorded.",
            action="Stop automated follow-up and recommend manual intervention to avoid damaging the relationship.",
        )

    outstanding = float(metrics.get("outstanding") or 0)
    amount = float(invoice.get("amount") or 0)
    if overdue_days > 0 and outstanding > 0 and amount >= outstanding * 0.3:
        record_anomaly(
            state,
            invoice,
            "Large overdue exposure",
            source=source,
            reasoning=f"{invoice['invoice_number']} is overdue and represents {amount / outstanding:.0%} of total outstanding AR.",
            action="Notify finance immediately and mark as high-priority collection risk.",
            level="error",
        )

    profile = invoice.get("risk_profile") or {}
    if profile.get("provider") == "exa" and int(profile.get("score", 0)) >= 75:
        record_anomaly(
            state,
            invoice,
            "Customer external risk signal",
            source=source,
            reasoning=profile.get("summary", f"Exa risk intelligence raised {client} to high risk."),
            action="Warn finance and recommend pausing new shipment until payment plan is confirmed.",
            level="error",
        )


def money_text(amount: float, currency: str = "USD") -> str:
    return f"{currency} {float(amount):,.2f}"


def command_window_days(command: str) -> int:
    text = command.lower()
    if "next week" in text or "this week" in text or "one week" in text or "1 week" in text:
        return 7
    if "next month" in text or "this month" in text:
        return 30
    return parse_forecast_window(command)


def command_amount_threshold(command: str) -> float:
    match = re.search(r"(?:over|above|greater than|more than)\s*\$?\s*([\d,]+(?:\.\d+)?)", command.lower())
    if not match:
        return 0.0
    return float(match.group(1).replace(",", ""))


def compact_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def known_client_names(state: dict[str, Any]) -> list[str]:
    clients = {
        str(item.get("client_name", "")).strip()
        for group in (state.get("invoices", []), state.get("po_pipeline", []))
        for item in group
        if item.get("client_name")
    }
    return sorted(clients, key=len, reverse=True)


def find_client_name(state: dict[str, Any], command: str) -> str:
    lowered = command.lower()
    compact_command = compact_name(command)
    clients = known_client_names(state)
    business_suffixes = {"inc", "llc", "ltd", "limited", "corp", "co", "company", "gmbh"}

    for client in clients:
        if client.lower() in lowered:
            return client

    for client in clients:
        compact_client = compact_name(client)
        if compact_client and compact_client in compact_command:
            return client
        for token in re.findall(r"[a-z0-9]+", client.lower()):
            if token in business_suffixes or len(token) < 4:
                continue
            if re.search(rf"\b{re.escape(token)}\b", lowered):
                return client
    return ""


def parse_scheduler_time(command: str) -> str | None:
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", command.lower())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    suffix = match.group(3)
    if suffix == "pm" and hour < 12:
        hour += 12
    if suffix == "am" and hour == 12:
        hour = 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        return None
    return f"{hour:02d}:{minute:02d}"


def parse_payment_terms(command: str) -> str:
    match = re.search(r"\bnet\s*(\d{1,3})\b", command.lower())
    return f"Net {int(match.group(1))}" if match else ""


def command_invoice_item(invoice: dict[str, Any]) -> dict[str, Any]:
    risk = invoice.get("risk_profile") or baseline_risk_profile(invoice)
    return {
        "invoice": invoice.get("invoice_number", ""),
        "client": invoice.get("client_name", ""),
        "amount": float(invoice.get("amount", 0)),
        "currency": invoice.get("currency", "USD"),
        "status": invoice.get("status", ""),
        "due_date": invoice.get("due_date", ""),
        "risk": f"{risk.get('level')} · {risk.get('score')}/100",
    }


def payment_status_payload(state: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    invoices = state.get("invoices", [])
    active = [i for i in invoices if i.get("status") != "paid"]
    paid = [i for i in invoices if i.get("status") == "paid"]
    overdue = [i for i in active if i.get("status") in {"overdue", "high_risk"} or days_overdue(i) > 0]
    stripe_payments = []
    today_text = today().isoformat()
    month_text = today_text[:7]
    for invoice in paid:
        details = invoice.get("payment_details") or stripe_record_for_paid_invoice(invoice)
        stripe_payments.append(
            {
                "invoice": invoice.get("invoice_number"),
                "client": invoice.get("client_name"),
                "amount": details.get("amount"),
                "currency": details.get("currency"),
                "method": details.get("method"),
                "paid_at": details.get("paid_at"),
                "provider": details.get("provider", "Stripe"),
                "settlement_status": details.get("settlement_status", "succeeded"),
            }
        )
    paid_today = [item for item in stripe_payments if str(item.get("paid_at", "")).startswith(today_text)]
    paid_this_month = [item for item in stripe_payments if str(item.get("paid_at", "")).startswith(month_text)]
    return {
        "paid_count": len(paid),
        "active_count": len(active),
        "overdue_count": len(overdue),
        "outstanding": metrics["outstanding"],
        "overdue": metrics["overdue"],
        "due_this_week": metrics["dueThisWeek"],
        "paid_this_month": metrics["paidThisMonth"],
        "paid_today": round(sum(float(item.get("amount") or 0) for item in paid_today), 2),
        "paid_today_count": len(paid_today),
        "stripe_payments": stripe_payments,
        "stripe_payments_today": paid_today,
        "stripe_payments_this_month": paid_this_month,
    }


def payment_status_message(command: str, payload: dict[str, Any]) -> tuple[str, list[str], dict[str, Any]]:
    normalized = command.lower()
    wants_today = "today" in normalized or "today's" in normalized
    wants_month = "month" in normalized
    if wants_today:
        payments = payload.get("stripe_payments_today", [])
        total = float(payload.get("paid_today") or 0)
        if not payments:
            return "No payments recorded today.", ["Source: Stripe payment records"], {**payload, "scope": "today"}
        if len(payments) == 1:
            payment = payments[0]
            message = (
                f"Today, {payment.get('client')} paid "
                f"{money_text(float(payment.get('amount') or 0), payment.get('currency', 'USD'))} "
                f"for {payment.get('invoice')}."
            )
        else:
            message = f"Today, {len(payments)} clients paid {money_text(total)}."
        details = [
            (
                f"{item.get('client')} · {item.get('invoice')} · "
                f"{money_text(float(item.get('amount') or 0), item.get('currency', 'USD'))} · "
                f"{item.get('provider', 'Stripe')} {item.get('method', 'payment')}"
            )
            for item in payments
        ]
        return message, details, {**payload, "scope": "today"}
    if wants_month:
        payments = payload.get("stripe_payments_this_month", [])
        return (
            f"This month, {len(payments)} payments total {money_text(float(payload.get('paid_this_month') or 0))}.",
            [
                (
                    f"{item.get('client')} · {item.get('invoice')} · "
                    f"{money_text(float(item.get('amount') or 0), item.get('currency', 'USD'))}"
                )
                for item in payments
            ] or ["No Stripe payments recorded this month."],
            {**payload, "scope": "month"},
        )
    return (
        f"{payload['paid_count']} paid, {payload['active_count']} active, {payload['overdue_count']} overdue/high-risk.",
        [
            f"Outstanding: {money_text(float(payload.get('outstanding') or 0))}",
            f"Overdue: {money_text(float(payload.get('overdue') or 0))}",
            f"Due this week: {money_text(float(payload.get('due_this_week') or 0))}",
        ],
        {**payload, "scope": "summary"},
    )


def riskiest_clients_payload(state: dict[str, Any]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for invoice in state.get("invoices", []):
        if invoice.get("status") == "paid":
            continue
        client = invoice.get("client_name", "Unknown")
        risk = invoice.get("risk_profile") or baseline_risk_profile(invoice)
        entry = grouped.setdefault(client, {"client": client, "amount": 0.0, "overdue": 0.0, "max_score": 0, "invoices": 0})
        amount = float(invoice.get("amount", 0))
        entry["amount"] += amount
        entry["invoices"] += 1
        entry["max_score"] = max(entry["max_score"], int(risk.get("score", 0)))
        if days_overdue(invoice) > 0:
            entry["overdue"] += amount
    items = sorted(grouped.values(), key=lambda item: (item["max_score"], item["overdue"], item["amount"]), reverse=True)
    return {"items": items[:5]}


def command_chase_invoices(
    state: dict[str, Any],
    invoices: list[dict[str, Any]],
    source: str,
    purpose: str,
    *,
    user_instruction: str = "",
) -> list[dict[str, Any]]:
    affected = []
    for invoice in invoices:
        ensure_agent_invoice_artifacts(state, invoice, source=source, purpose=purpose)
        draft = create_draft(
            state,
            invoice,
            purpose,
            source=source,
            reasoning=invoice_decision_reason(invoice, purpose),
            user_instruction=user_instruction,
        )
        affected.append(
            {
                "invoice": invoice.get("invoice_number", ""),
                "client": invoice.get("client_name", ""),
                "amount": float(invoice.get("amount", 0)),
                "currency": invoice.get("currency", "USD"),
                "status": draft.get("status", "awaiting_confirmation"),
                "draft_id": draft.get("id", ""),
                "subject": draft.get("subject", ""),
                "body": draft.get("body", ""),
                "priority": draft.get("priority", "P2"),
                "priority_label": draft.get("priority_label", "Routine"),
                "client_email": draft.get("client_email", ""),
            }
        )
    return affected


def is_overdue_notification_command(command: str) -> bool:
    normalized = command.lower()
    action_terms = ["send", "notify", "notification", "notice", "email", "remind", "reminder", "chase", "follow up", "follow-up"]
    return "overdue" in normalized and any(term in normalized for term in action_terms)


def is_overdue_amount_query(command: str) -> bool:
    normalized = command.lower()
    return "overdue" in normalized and (
        bool(re.search(r"\b(over|above|greater than|more than)\b", normalized)) or "$" in normalized
    )


def is_due_window_query(command: str) -> bool:
    normalized = command.lower()
    action_terms = ["send", "notify", "notification", "notice", "email", "remind", "reminder", "chase", "follow up", "follow-up"]
    if any(term in normalized for term in action_terms):
        return False
    if "due" not in normalized:
        return False
    return any(term in normalized for term in ["week", "7 day", "seven day", "next few days", "coming days", "upcoming"])


def due_window_payload(state: dict[str, Any], horizon_days: int) -> dict[str, Any]:
    end_date = today() + dt.timedelta(days=horizon_days)
    items = []
    for invoice in state.get("invoices", []):
        if invoice.get("status") == "paid":
            continue
        due = parse_date(invoice.get("due_date"))
        if not due or due < today() or due > end_date:
            continue
        items.append(command_invoice_item(invoice))
    items.sort(key=lambda item: item.get("due_date", ""))
    return {
        "horizon_days": horizon_days,
        "end_date": end_date.isoformat(),
        "items": items,
        "total": round(sum(float(item.get("amount") or 0) for item in items), 2),
    }


def client_receivable_summary(state: dict[str, Any], client: str) -> dict[str, Any]:
    invoices = [i for i in state.get("invoices", []) if i.get("client_name") == client and i.get("status") != "paid"]
    overdue_total = sum(float(i.get("amount", 0)) for i in invoices if days_overdue(i) > 0)
    due_soon_total = sum(
        float(i.get("amount", 0))
        for i in invoices
        if parse_date(i.get("due_date")) and 0 <= (parse_date(i.get("due_date")) - today()).days <= 14
    )
    max_overdue = max([days_overdue(i) for i in invoices] or [0])
    total = sum(float(i.get("amount", 0)) for i in invoices)
    risk_scores = [int((i.get("risk_profile") or baseline_risk_profile(i)).get("score", 0)) for i in invoices]
    return {
        "client": client,
        "active_invoices": len(invoices),
        "open_total": total,
        "overdue_total": overdue_total,
        "due_soon_total": due_soon_total,
        "max_days_overdue": max_overdue,
        "max_risk_score": max(risk_scores or [0]),
        "sent_followups": sum(int(i.get("email_sent_count", 0)) for i in invoices),
    }


def discount_candidate_score(candidate: dict[str, Any]) -> float:
    score = 0.0
    score += min(candidate["due_soon_total"], 50000) / 1000 * 1.2
    score += min(candidate["overdue_total"], 50000) / 1000 * 0.8
    score -= max(candidate["max_days_overdue"] - 21, 0) * 2.0
    score -= max(candidate["max_risk_score"] - 65, 0) * 1.5
    score += candidate["active_invoices"] * 4
    return score


def discount_recommendation_for_clients(state: dict[str, Any], command: str) -> dict[str, Any]:
    candidates = [
        client_receivable_summary(state, client)
        for client in known_client_names(state)
        if client_receivable_summary(state, client)["active_invoices"] > 0
    ]
    for candidate in candidates:
        candidate["discount_score"] = round(discount_candidate_score(candidate), 2)
    candidates = sorted(candidates, key=lambda item: item["discount_score"], reverse=True)
    top = candidates[0] if candidates else {
        "client": "No active customer",
        "active_invoices": 0,
        "overdue_total": 0,
        "max_days_overdue": 0,
    }
    recommendation = (
        f"Start with {top['client']}." if candidates else "No active receivables are available for an early-payment discount."
    )
    reasoning = (
        f"{top['client']} has {top.get('active_invoices', 0)} active invoices, "
        f"{money_text(top.get('overdue_total', 0))} overdue, and a maximum overdue age of "
        f"{top.get('max_days_overdue', 0)} days. The agent prioritizes customers where a discount can accelerate cash without masking severe collection risk."
    )
    provider = "rules"
    openai_error = ""
    if llm_configured() and candidates:
        prompt = f"""
You are an accounts receivable agent for HelloBike Manufacturing Co.
The user is asking which customer should receive an early-payment discount offer.
Use ONLY the computed candidate data. Do not invent invoices, amounts, or customer facts.
Pick one customer and explain why. If no customer is appropriate, say so.
Return ONLY JSON with keys: client, recommendation, reasoning.

User command:
{command}

Candidates:
{json.dumps(candidates, ensure_ascii=False)}
"""
        try:
            cleaned = openai_response(prompt, temperature=0.2).strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
                cleaned = re.sub(r"```$", "", cleaned).strip()
            ai = json.loads(cleaned)
            ai_client = str(ai.get("client") or top["client"])
            selected = next((item for item in candidates if item["client"].lower() == ai_client.lower()), top)
            top = selected
            recommendation = str(ai.get("recommendation") or recommendation)
            reasoning = str(ai.get("reasoning") or reasoning)
            provider = llm_provider()
        except Exception as exc:
            openai_error = safe_openai_error(exc)
    return {
        "client": top.get("client", "Recommendation"),
        "recommendation": recommendation,
        "reasoning": reasoning,
        "overdue_total": top.get("overdue_total", 0),
        "active_invoices": top.get("active_invoices", 0),
        "max_days_overdue": top.get("max_days_overdue", 0),
        "provider": provider,
        "openai_error": openai_error,
        "candidates": candidates[:5],
    }


def risk_recommendation_for_clients(state: dict[str, Any], command: str) -> dict[str, Any]:
    candidates = [
        client_receivable_summary(state, client)
        for client in known_client_names(state)
        if client_receivable_summary(state, client)["active_invoices"] > 0
    ]
    for candidate in candidates:
        candidate["risk_score"] = round(
            candidate["max_risk_score"]
            + min(candidate["overdue_total"], 50000) / 1000
            + max(candidate["max_days_overdue"], 0) * 1.2,
            2,
        )
    candidates = sorted(candidates, key=lambda item: item["risk_score"], reverse=True)
    top = candidates[0] if candidates else {
        "client": "No active customer",
        "active_invoices": 0,
        "overdue_total": 0,
        "max_days_overdue": 0,
        "max_risk_score": 0,
    }
    recommendation = (
        f"{top['client']} is currently the highest-risk customer."
        if candidates
        else "No active customer risk found."
    )
    reasoning = (
        f"{top['client']} has {top.get('active_invoices', 0)} active invoices, "
        f"{money_text(top.get('overdue_total', 0))} overdue, max overdue age of "
        f"{top.get('max_days_overdue', 0)} days, and max invoice risk score "
        f"{top.get('max_risk_score', 0)}/100."
    )
    provider = "rules"
    openai_error = ""
    if llm_configured() and candidates:
        prompt = f"""
You are an accounts receivable risk analyst for HelloBike Manufacturing Co.
The user asks which customer is most at risk.
Use ONLY the computed receivables data. Do not invent news, payments, or invoices.
Pick one customer and explain the risk in CFO-friendly language.
Return ONLY JSON with keys: client, recommendation, reasoning.

User command:
{command}

Candidates:
{json.dumps(candidates, ensure_ascii=False)}
"""
        try:
            cleaned = openai_response(prompt, temperature=0.2).strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
                cleaned = re.sub(r"```$", "", cleaned).strip()
            ai = json.loads(cleaned)
            ai_client = str(ai.get("client") or top["client"])
            selected = next((item for item in candidates if item["client"].lower() == ai_client.lower()), top)
            top = selected
            recommendation = str(ai.get("recommendation") or recommendation)
            reasoning = str(ai.get("reasoning") or reasoning)
            provider = llm_provider()
        except Exception as exc:
            openai_error = safe_openai_error(exc)
    return {
        "client": top.get("client", "Recommendation"),
        "recommendation": recommendation,
        "reasoning": reasoning,
        "overdue_total": top.get("overdue_total", 0),
        "active_invoices": top.get("active_invoices", 0),
        "max_days_overdue": top.get("max_days_overdue", 0),
        "provider": provider,
        "openai_error": openai_error,
        "candidates": candidates[:5],
    }


def judgment_for_client(state: dict[str, Any], client: str, command: str) -> dict[str, Any]:
    invoices = [i for i in state.get("invoices", []) if i.get("client_name") == client and i.get("status") != "paid"]
    overdue_total = sum(float(i.get("amount", 0)) for i in invoices if days_overdue(i) > 0)
    max_overdue = max([days_overdue(i) for i in invoices] or [0])
    sent_count = sum(int(i.get("email_sent_count", 0)) for i in invoices)
    invoice_context = [
        {
            "invoice_number": i.get("invoice_number"),
            "po_number": i.get("po_number"),
            "amount": float(i.get("amount", 0)),
            "currency": i.get("currency", "USD"),
            "status": i.get("status"),
            "invoice_date": i.get("invoice_date"),
            "shipment_date": i.get("shipment_date"),
            "due_date": i.get("due_date"),
            "days_overdue": days_overdue(i),
            "email_sent_count": int(i.get("email_sent_count", 0)),
            "risk_profile": i.get("risk_profile") or baseline_risk_profile(i),
        }
        for i in invoices
    ]
    if "discount" in command.lower():
        recommendation = "Offer a small early-payment discount only if payment can be collected this week."
        reasoning = f"{client} has {len(invoices)} active invoices and {money_text(overdue_total)} overdue. Discount can improve cash timing, but should not replace collection discipline."
    elif "write" in command.lower():
        recommendation = "Do not write it off yet; move to manual intervention if no response after the next firm notice."
        reasoning = f"{client}'s longest overdue invoice is {max_overdue} days overdue with {sent_count} sent follow-ups. There is not enough evidence for write-off unless external risk or no-response history worsens."
    else:
        recommendation = "Keep chasing with a relationship-preserving tone."
        reasoning = f"{client} has active receivables that still look collectible based on current invoice state."
    provider = "rules"
    openai_error = ""
    if llm_configured():
        prompt = f"""
You are an accounts receivable agent for HelloBike Manufacturing Co.
Answer the user's finance judgment question using ONLY the supplied receivables data.
Do not invent invoice amounts, due dates, payment status, or risk scores.
If an action should be taken, phrase it as a recommendation that still requires human approval.
Return ONLY JSON with keys: recommendation, reasoning.

User command:
{command}

Client:
{client}

Computed metrics:
{json.dumps({
    "active_invoices": len(invoices),
    "overdue_total": overdue_total,
    "max_days_overdue": max_overdue,
    "sent_followups": sent_count,
}, ensure_ascii=False)}

Invoices:
{json.dumps(invoice_context, ensure_ascii=False)}
"""
        try:
            cleaned = openai_response(prompt, temperature=0.2).strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
                cleaned = re.sub(r"```$", "", cleaned).strip()
            ai = json.loads(cleaned)
            recommendation = str(ai.get("recommendation") or recommendation)
            reasoning = str(ai.get("reasoning") or reasoning)
            provider = llm_provider()
        except Exception as exc:
            openai_error = safe_openai_error(exc)
    return {
        "client": client,
        "recommendation": recommendation,
        "reasoning": reasoning,
        "overdue_total": overdue_total,
        "active_invoices": len(invoices),
        "max_days_overdue": max_overdue,
        "provider": provider,
        "openai_error": openai_error,
    }


def lynn_briefing(state: dict[str, Any]) -> dict[str, Any]:
    invoices = state.get("invoices", [])
    forecast_records = forecast_records_from_state(state)
    active = [item for item in invoices if item.get("status") != "paid"]
    paid = [item for item in invoices if item.get("status") == "paid"]
    overdue = [item for item in active if days_overdue(item) > 0 or item.get("status") in {"overdue", "high_risk"}]
    due_week = [
        item
        for item in active
        if parse_date(item.get("due_date")) and 0 <= (parse_date(item.get("due_date")) - today()).days <= 7
    ]
    pipeline = state.get("po_pipeline", [])
    ready_pos = [item for item in pipeline if item.get("status") == "ready_to_invoice"]
    upcoming_pos = [item for item in pipeline if item.get("status") == "upcoming"]
    blocked_pos = [item for item in pipeline if item.get("status") in {"needs_review", "blocked"}]
    pending = [item for item in state.get("drafts", []) if item.get("status") == "awaiting_confirmation"]
    p0 = [item for item in pending if item.get("priority") == "P0"]
    p1 = [item for item in pending if item.get("priority") == "P1"]
    forecast = forecast_cash_flow(forecast_records, 90)
    top_risk = None
    if overdue:
        top_risk = sorted(overdue, key=lambda item: (days_overdue(item), float(item.get("amount", 0))), reverse=True)[0]
    if not invoices and not pipeline:
        headline = "Lynn is waiting for today's PO tracker."
        summary = "Once data is available, she will read shipment dates, open invoices, Stripe payment status, and prepare approvals."
    else:
        headline = f"Lynn checked {len(active)} active invoices and {len(pipeline)} POs."
        summary = (
            f"{len(overdue)} overdue, {len(due_week)} due this week, "
            f"{len(pending)} approvals waiting, {len(p0)} critical."
        )
    return {
        "headline": headline,
        "summary": summary,
        "next_best_action": (
            f"Review {len(p0)} P0 item first." if p0 else
            f"Review {len(p1)} P1 item next." if p1 else
            "No urgent approval is waiting."
        ),
        "stats": {
            "active_invoices": len(active),
            "paid_invoices": len(paid),
            "overdue_invoices": len(overdue),
            "due_this_week": len(due_week),
            "pending_approvals": len(pending),
            "critical_approvals": len(p0),
            "ready_pos": len(ready_pos),
            "upcoming_pos": len(upcoming_pos),
            "blocked_pos": len(blocked_pos),
            "forecast_30d": forecast.get("risk_adjusted", 0),
            "forecast_90d": forecast.get("risk_adjusted", 0),
        },
        "top_risk": {
            "invoice_number": top_risk.get("invoice_number", ""),
            "client_name": top_risk.get("client_name", ""),
            "amount": float(top_risk.get("amount", 0)),
            "currency": top_risk.get("currency", "USD"),
            "days_overdue": days_overdue(top_risk),
        } if top_risk else None,
        "updated_at": now_iso(),
    }


def state_payload(state: dict[str, Any], **extra: Any) -> dict[str, Any]:
    invoices = state.get("invoices", [])
    forecast_records = forecast_records_from_state(state)
    payload = {
        **state,
        "metrics": invoice_metrics(invoices),
        "cashflowForecast": forecast_cash_flow(forecast_records, 90),
        "lynnBriefing": lynn_briefing(state),
        "config": config_status(),
    }
    payload.update(extra)
    return payload


def find_invoice(state: dict[str, Any], invoice_id: str) -> dict[str, Any]:
    for invoice in state.get("invoices", []):
        if invoice.get("id") == invoice_id:
            return invoice
    raise KeyError(invoice_id)


def cents(amount: float) -> int:
    return int(round(float(amount) * 100))


def stripe_post(path: str, params: dict[str, Any]) -> dict[str, Any]:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    encoded = parse.urlencode(params).encode("utf-8")
    auth = base64.b64encode(f"{key}:".encode("utf-8")).decode("ascii")
    req = request.Request(
        f"https://api.stripe.com/v1/{path.lstrip('/')}",
        data=encoded,
        headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Stripe API error {exc.code}: {detail}") from exc


def stripe_get(path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    key = os.environ.get("STRIPE_SECRET_KEY")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not configured")
    query = f"?{parse.urlencode(params or {})}" if params else ""
    auth = base64.b64encode(f"{key}:".encode("utf-8")).decode("ascii")
    req = request.Request(
        f"https://api.stripe.com/v1/{path.lstrip('/')}{query}",
        headers={"Authorization": f"Basic {auth}"},
        method="GET",
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Stripe API error {exc.code}: {detail}") from exc


def create_stripe_link(invoice: dict[str, Any]) -> dict[str, Any]:
    if invoice.get("stripe_payment_link") and invoice.get("stripe_payment_link_id"):
        return {
            "id": invoice.get("stripe_payment_link_id"),
            "url": invoice.get("stripe_payment_link"),
            "reused": True,
        }
    price = stripe_post(
        "prices",
        {
            "currency": invoice.get("currency", "USD").lower(),
            "unit_amount": cents(invoice.get("amount", 0)),
            "product_data[name]": f"{invoice['invoice_number']} - {invoice['client_name']}",
            "metadata[invoice_id]": invoice["id"],
            "metadata[invoice_number]": invoice["invoice_number"],
            "metadata[po_number]": invoice.get("po_number", ""),
            "metadata[client_name]": invoice.get("client_name", ""),
        },
    )
    payment_link = stripe_post(
        "payment_links",
        {
            "line_items[0][price]": price["id"],
            "line_items[0][quantity]": 1,
            "metadata[invoice_id]": invoice["id"],
            "metadata[invoice_number]": invoice["invoice_number"],
            "metadata[po_number]": invoice.get("po_number", ""),
            "metadata[client_name]": invoice.get("client_name", ""),
        },
    )
    invoice["stripe_payment_link"] = payment_link.get("url", "")
    invoice["stripe_payment_link_id"] = payment_link.get("id", "")
    return payment_link


def ensure_agent_invoice_artifacts(
    state: dict[str, Any],
    invoice: dict[str, Any],
    *,
    source: str,
    purpose: str,
    log_activity: bool = True,
) -> None:
    if not invoice.get("risk_profile"):
        profile = update_invoice_risk(invoice)
        if log_activity:
            add_activity(
                state,
                f"Agent checked risk for {invoice['invoice_number']}.",
                source=source,
                kind="Tool Use",
                reasoning=f"{purpose} requires tone selection and escalation logic before drafting.",
                outcome=f"Risk score {profile.get('score')}/100; tone set to {profile.get('recommended_tone')}.",
            )

    if not invoice.get("pdf_path"):
        pdf_path = generate_invoice_pdf(invoice)
        invoice["pdf_path"] = pdf_path
        if log_activity:
            add_activity(
                state,
                f"Agent generated invoice PDF for {invoice['invoice_number']}.",
                source=source,
                kind="Tool Use",
                reasoning="Customer-facing follow-up should include a company invoice PDF and exclude internal risk notes.",
                outcome=f"PDF attached: {pdf_path}",
            )

    if not invoice.get("stripe_payment_link"):
        if os.environ.get("STRIPE_SECRET_KEY"):
            try:
                payment_link = create_stripe_link(invoice)
                update_invoice_drafts_payment_link(state, invoice)
                if log_activity:
                    add_activity(
                        state,
                        f"Agent created Stripe payment link for {invoice['invoice_number']}.",
                        source=source,
                        kind="Tool Use",
                        reasoning="Payment collection follow-up needs a secure payment link before human approval.",
                        outcome=f"Stripe payment link ready: {payment_link.get('url', 'created')}",
                    )
            except Exception as exc:
                if log_activity:
                    add_activity(
                        state,
                        f"Stripe link creation failed for {invoice['invoice_number']}.",
                        "warn",
                        source=source,
                        kind="Tool Failure",
                        reasoning="The agent attempted to create a Stripe payment link automatically.",
                        outcome=f"Draft can continue, but payment link needs attention: {exc}",
                    )
        else:
            if log_activity:
                add_activity(
                    state,
                    f"Stripe key missing for {invoice['invoice_number']}.",
                    "warn",
                    source=source,
                    kind="Tool Guardrail",
                    reasoning="The agent would create a Stripe payment link automatically, but STRIPE_SECRET_KEY is not configured.",
                    outcome="Draft will show payment link pending until Stripe is configured.",
                )


def baseline_risk_profile(invoice: dict[str, Any]) -> dict[str, Any]:
    overdue_days = days_overdue(invoice)
    if overdue_days >= 30:
        score = 82
        level = "High"
        tone = "Firm escalation with manual review"
        action = "Escalate to finance lead, request payment date, and prepare manual intervention."
        tone_options = ["Firm escalation", "Executive escalation", "Final notice"]
        summary = f"{invoice['invoice_number']} is {overdue_days} days overdue, crossing the 30-day high-risk threshold."
    elif overdue_days >= 7:
        score = 58
        level = "Medium"
        tone = "Firm but relationship-preserving"
        action = "Send a firm follow-up and ask for a confirmed payment timeline."
        tone_options = ["Firm reminder", "Relationship-preserving", "Second notice"]
        summary = f"{invoice['invoice_number']} is {overdue_days} days overdue. Follow-up should be more direct."
    elif overdue_days > 0:
        score = 38
        level = "Low-Medium"
        tone = "Friendly reminder"
        action = "Send a friendly overdue reminder with payment link."
        tone_options = ["Friendly reminder", "Helpful nudge", "First notice"]
        summary = f"{invoice['invoice_number']} is {overdue_days} days overdue. Early reminder is appropriate."
    else:
        score = 18
        level = "Low"
        tone = "Friendly pre-due reminder"
        action = "No escalation. Send pre-due reminder only if invoice is approaching due date."
        tone_options = ["Friendly pre-due reminder", "Standard invoice note", "Light touch"]
        summary = f"{invoice['invoice_number']} is not overdue. No collection escalation is needed."
    return {
        "score": score,
        "level": level,
        "summary": summary,
        "recommended_action": action,
        "recommended_tone": tone,
        "tone_options": tone_options,
        "signals": [f"Days overdue: {overdue_days}"],
        "sources": [],
        "provider": "baseline",
        "checked_at": now_iso(),
    }


def exa_search(query: str) -> list[dict[str, Any]]:
    api_key = os.environ.get("EXA_API_KEY")
    if not api_key:
        return []
    body = {
        "query": query,
        "numResults": 5,
        "contents": {"text": True, "highlights": True},
    }
    req = request.Request(
        "https://api.exa.ai/search",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Exa API error {exc.code}: {detail}") from exc
    return payload.get("results", [])


def risk_from_exa(invoice: dict[str, Any]) -> dict[str, Any]:
    base = baseline_risk_profile(invoice)
    query = (
        f"{invoice['client_name']} company latest news financial difficulties bankruptcy "
        f"operations business status"
    )
    results = exa_search(query)
    if not results:
        return base

    source_notes = []
    negative_terms = ["bankruptcy", "insolvency", "lawsuit", "shutdown", "layoff", "default", "debt", "distress"]
    score_boost = 0
    for result in results[:5]:
        title = result.get("title", "")
        url = result.get("url", "")
        highlights = " ".join(result.get("highlights", []) or [])
        text = f"{title} {highlights}".lower()
        hits = [term for term in negative_terms if term in text]
        if hits:
            score_boost += min(20, 8 * len(hits))
        source_notes.append({"title": title or url, "url": url})

    score = min(100, int(base["score"]) + score_boost)
    level = "High" if score >= 75 else "Medium" if score >= 45 else "Low"
    tone = "Firm escalation with manual review" if score >= 75 else "Firm but relationship-preserving" if score >= 45 else "Friendly reminder"
    action = base.get("recommended_action", "Standard follow-up")
    tone_options = base.get("tone_options", [])
    summary = base["summary"]

    if llm_configured():
        compact_results = [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "highlights": r.get("highlights", [])[:2],
                "text": (r.get("text") or "")[:800],
            }
            for r in results[:4]
        ]
        prompt = f"""
You are an accounts receivable risk analyst.
Use the Exa search results to summarize collection risk for this overdue invoice.
Return ONLY JSON with keys: score (0-100 integer), level, summary, recommended_action, recommended_tone, tone_options, signals.

Invoice:
{json.dumps(invoice, ensure_ascii=False)}

Baseline:
{json.dumps(base, ensure_ascii=False)}

Exa search results:
{json.dumps(compact_results, ensure_ascii=False)}
"""
        try:
            cleaned = openai_response(prompt).strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
                cleaned = re.sub(r"```$", "", cleaned).strip()
            ai_profile = json.loads(cleaned)
            score = int(ai_profile.get("score", score))
            level = str(ai_profile.get("level", level))
            summary = str(ai_profile.get("summary", summary))
            action = str(ai_profile.get("recommended_action", action))
            tone = str(ai_profile.get("recommended_tone", tone))
            tone_options = ai_profile.get("tone_options") if isinstance(ai_profile.get("tone_options"), list) else tone_options
            signals = ai_profile.get("signals") if isinstance(ai_profile.get("signals"), list) else base["signals"]
        except Exception:
            signals = base["signals"]
    else:
        signals = base["signals"] + ([f"Exa negative-signal boost: +{score_boost}"] if score_boost else ["Exa found no obvious negative signals in top results."])

    return {
        "score": score,
        "level": level,
        "summary": summary,
        "recommended_action": action,
        "recommended_tone": tone,
        "tone_options": tone_options,
        "signals": signals,
        "sources": source_notes,
        "provider": "exa",
        "checked_at": now_iso(),
    }


def update_invoice_risk(invoice: dict[str, Any]) -> dict[str, Any]:
    profile = risk_from_exa(invoice) if os.environ.get("EXA_API_KEY") else baseline_risk_profile(invoice)
    invoice["risk_profile"] = profile
    if invoice.get("status") not in {"paid", "partial_paid"}:
        due = parse_date(invoice.get("due_date"))
        if due:
            invoice["status"] = status_for_due(due, risk_score=int(profile.get("score", 0)))
    return profile


def mark_invoice_paid(
    invoice: dict[str, Any],
    *,
    method: str = "card",
    reference: str = "simulated_stripe_payment",
    amount: float | None = None,
    currency: str | None = None,
) -> dict[str, Any]:
    paid_at = now_iso()
    settled_currency = (currency or invoice.get("currency") or "USD").upper()
    invoice_amount = round(float(invoice.get("amount", 0)), 2)
    paid_amount = round(float(amount if amount is not None else invoice.get("amount", 0)), 2)
    remaining = round(max(invoice_amount - paid_amount, 0), 2)
    invoice["status"] = "paid" if remaining == 0 else "partial_paid"
    invoice["paid_at"] = paid_at
    invoice["payment_details"] = {
        "provider": "Stripe",
        "amount": paid_amount,
        "currency": settled_currency,
        "method": method,
        "paid_at": paid_at,
        "reference": reference,
        "settlement_status": "succeeded" if remaining == 0 else "partial",
        "remaining_amount": remaining,
    }
    return invoice["payment_details"]


def close_pending_drafts_for_paid_invoice(state: dict[str, Any], invoice: dict[str, Any]) -> int:
    closed = 0
    invoice_id = invoice.get("id")
    for draft in state.get("drafts", []):
        same_invoice = draft.get("invoice_id") == invoice_id
        same_payment_link = (
            invoice.get("stripe_payment_link_id")
            and draft.get("payment_link_id") == invoice.get("stripe_payment_link_id")
        )
        if (same_invoice or same_payment_link) and draft.get("status") == "awaiting_confirmation":
            draft["status"] = "paid_closed"
            draft["closed_at"] = now_iso()
            draft["closed_reason"] = "Stripe confirmed payment"
            draft["invoice_id"] = invoice_id
            closed += 1
    return closed


def invoice_for_stripe_payment_link(state: dict[str, Any], payment_link_id: str) -> dict[str, Any] | None:
    if not payment_link_id:
        return None
    for invoice in state.get("invoices", []):
        if invoice.get("stripe_payment_link_id") == payment_link_id:
            return invoice
    for draft in state.get("drafts", []):
        if draft.get("payment_link_id") != payment_link_id or draft.get("approval_type") != "create_invoice":
            continue
        po = next((item for item in state.get("po_pipeline", []) if item.get("id") == draft.get("po_id")), None)
        if not po:
            continue
        invoice = invoice_from_po(po)
        invoice["stripe_payment_link"] = draft.get("payment_link", "")
        invoice["stripe_payment_link_id"] = draft.get("payment_link_id", "")
        invoice["pdf_path"] = draft.get("attachment_url", "") or invoice.get("pdf_path", "")
        state.setdefault("invoices", []).append(invoice)
        po["status"] = "invoiced"
        po["approved_at"] = now_iso()
        po["invoice_id"] = invoice["id"]
        draft["invoice_id"] = invoice["id"]
        return invoice
    return None


def apply_stripe_checkout_session(state: dict[str, Any], session: dict[str, Any], source: str = "Stripe Payment") -> bool:
    payment_link_id = session.get("payment_link") or ""
    invoice = invoice_for_stripe_payment_link(state, payment_link_id)
    if not invoice:
        invoice_id = session.get("metadata", {}).get("invoice_id")
        invoice = next((item for item in state.get("invoices", []) if item.get("id") == invoice_id), None)
    if not invoice:
        return False
    reference = str(session.get("payment_intent") or session.get("id") or "stripe_checkout_session")
    existing_reference = (invoice.get("payment_details") or {}).get("reference")
    if invoice.get("status") == "paid" and existing_reference == reference:
        return False
    amount_total = session.get("amount_total")
    amount = (float(amount_total) / 100) if amount_total is not None else float(invoice.get("amount", 0))
    method_types = session.get("payment_method_types") or []
    method = method_types[0] if method_types else session.get("payment_method", "card")
    payment = mark_invoice_paid(
        invoice,
        method=str(method),
        reference=reference,
        amount=amount,
        currency=str(session.get("currency") or invoice.get("currency", "USD")).upper(),
    )
    if payment.get("remaining_amount", 0) == 0:
        closed_drafts = close_pending_drafts_for_paid_invoice(state, invoice)
    else:
        closed_drafts = 0
        ensure_agent_invoice_artifacts(
            state,
            invoice,
            source=source,
            purpose="partial payment follow-up",
            log_activity=False,
        )
        create_draft(
            state,
            invoice,
            "partial payment follow-up",
            source=source,
            reasoning=invoice_decision_reason(invoice, "partial payment follow-up"),
            log_activity=False,
        )
        record_anomaly(
            state,
            invoice,
            "Partial payment",
            source=source,
            reasoning=invoice_decision_reason(invoice, "partial payment follow-up"),
            action=f"Prepared a balance follow-up for the remaining {invoice.get('currency', 'USD')} {payment['remaining_amount']:,.2f}; waiting for your OK.",
        )
    add_activity(
        state,
        f"Payment of ${payment['amount']:,.2f} received from {invoice['client_name']} via Stripe.",
        source=source,
        kind="Payment Reasoning",
        reasoning="Lynn matched the Stripe payment to the invoice payment link and compared received amount with invoice amount.",
        outcome=(
            f"{invoice['invoice_number']} marked paid; {closed_drafts} pending item{'' if closed_drafts == 1 else 's'} closed."
            if payment.get("remaining_amount", 0) == 0
            else f"Partial payment flagged; ${payment['remaining_amount']:,.2f} remains outstanding and needs follow-up."
        ),
    )
    return True


def sync_stripe_payments(state: dict[str, Any]) -> dict[str, Any]:
    link_ids = {
        str(item.get("stripe_payment_link_id"))
        for item in state.get("invoices", [])
        if item.get("stripe_payment_link_id")
    }
    link_ids.update(
        str(item.get("payment_link_id"))
        for item in state.get("drafts", [])
        if item.get("payment_link_id")
    )
    checked = 0
    applied = 0
    errors: list[str] = []
    for link_id in sorted(link_ids):
        try:
            sessions = stripe_get("checkout/sessions", {"limit": 10, "payment_link": link_id})
        except Exception as exc:
            errors.append(safe_openai_error(exc))
            continue
        for session in sessions.get("data", []):
            checked += 1
            if session.get("payment_status") == "paid" or session.get("status") == "complete":
                if apply_stripe_checkout_session(state, session):
                    applied += 1
    return {"checked": checked, "applied": applied, "errors": errors}



def draft_email(invoice: dict[str, Any], purpose: str, user_instruction: str = "") -> str:
    risk = invoice.get("risk_profile") or baseline_risk_profile(invoice)
    if not llm_configured():
        return template_email(invoice, purpose)
    email_risk_context = dict(risk)
    email_risk_context.pop("recommended_tone", None)
    user_context = user_instruction.strip() or "No additional user instruction."
    prompt = f"""
Write a concise B2B accounts receivable email.
Sender: {os.environ.get("SENDER_EMAIL", "finance@example.com")}
Purpose: {purpose}
User's original Ask Lynn request:
{user_context}

Tone rules:
- Professional and clear.
- Interpret tone naturally from the user's original Ask Lynn request.
- Do not invent invoice facts, payment status, promises, or customer responses.
- Mention invoice number, PO number, due date, amount, and payment link if present.
- Keep it under 150 words.
- Return only the email body, no subject line.

Invoice:
{json.dumps(invoice, ensure_ascii=False)}

Risk profile:
{json.dumps(email_risk_context, ensure_ascii=False)}
"""
    try:
        return openai_response(prompt)
    except Exception:
        return template_email(invoice, purpose)


def email_with_payment_link(invoice: dict[str, Any], body: str) -> str:
    link = invoice.get("stripe_payment_link") or "Stripe payment link pending"
    if link != "Stripe payment link pending":
        updated = re.sub(
            r"Stripe payment link pending|Payment link pending",
            link,
            body,
            flags=re.IGNORECASE,
        )
        if updated != body:
            return updated
    if re.search(r"(?:pay|buy)\.stripe\.com|stripe payment link pending|payment link pending", body, re.IGNORECASE):
        return body
    return f"{body.rstrip()}\n\nPayment link: {link}"


def update_invoice_drafts_payment_link(state: dict[str, Any], invoice: dict[str, Any]) -> None:
    link = invoice.get("stripe_payment_link")
    if not link:
        return
    for draft in state.get("drafts", []):
        if draft.get("invoice_id") != invoice.get("id"):
            continue
        draft["payment_link"] = link
        if draft.get("body"):
            draft["body"] = email_with_payment_link(invoice, draft["body"])


def template_email(invoice: dict[str, Any], purpose: str) -> str:
    link = invoice.get("stripe_payment_link") or "Stripe payment link pending"
    return (
        f"Hi {invoice.get('client_name', 'there')},\n\n"
        f"This is a quick note regarding {invoice.get('invoice_number')} for "
        f"{invoice.get('currency', 'USD')} {float(invoice.get('amount', 0)):,.2f}, "
        f"related to {invoice.get('po_number')} and due on {invoice.get('due_date')}.\n\n"
        f"You can pay securely here: {link}\n\n"
        "Please let us know if you need anything else from our side.\n\n"
        "Best,\nFinance Team"
    )


def draft_dedupe_key(invoice: dict[str, Any], purpose: str) -> str:
    scope = "single" if purpose == "invoice email" else today().isoformat()
    return f"{invoice['id']}:{purpose}:{scope}"


def create_draft(
    state: dict[str, Any],
    invoice: dict[str, Any],
    purpose: str,
    *,
    source: str = "Agent Reasoning",
    reasoning: str = "",
    user_instruction: str = "",
    log_activity: bool = True,
) -> dict[str, Any]:
    paused = {str(client).lower() for client in state.get("paused_clients", [])}
    if str(invoice.get("client_name", "")).lower() in paused:
        if log_activity:
            add_activity(
                state,
                f"Email automation is paused for {invoice['client_name']}.",
                "warn",
                source=source,
                kind="Automation Pause",
                reasoning=f"{invoice['client_name']} is on the paused-client list from a user command.",
                outcome="No new draft was created for this customer.",
            )
        return {
            "id": "",
            "invoice_id": invoice["id"],
            "purpose": purpose,
            "status": "paused",
        }
    key = draft_dedupe_key(invoice, purpose)
    for draft in state.setdefault("drafts", []):
        legacy_invoice_email = (
            purpose == "invoice email"
            and draft.get("invoice_id") == invoice["id"]
            and draft.get("subject", "").lower().startswith("invoice email")
        )
        same_key = draft.get("dedupe_key") == key
        if (same_key or legacy_invoice_email) and draft.get("status") in {"awaiting_confirmation", "sent"}:
            status_text = "waiting for confirmation" if draft.get("status") == "awaiting_confirmation" else "already sent"
            if log_activity:
                add_activity(
                    state,
                    f"{invoice['invoice_number']} already has a {purpose} {status_text}.",
                    "warn",
                    source=source,
                    kind="Duplicate Guard",
                    reasoning="The agent blocks repeated follow-ups for the same invoice and purpose to avoid spamming the customer.",
                    outcome="No new email draft was created.",
                )
            return draft
        if (same_key or legacy_invoice_email) and draft.get("status") == "held" and held_until_is_future(draft):
            if log_activity:
                add_activity(
                    state,
                    f"{invoice['invoice_number']} {purpose} is held until the next Morning check-in.",
                    "warn",
                    source=source,
                    kind="Held Draft",
                    reasoning="The user chose Hold for now, so Lynn waits until the next scheduled 09:00 check before proposing it again.",
                    outcome="No new draft was created.",
                )
            return draft

    if purpose == "invoice email" and not invoice.get("pdf_path"):
        invoice["pdf_path"] = generate_invoice_pdf(invoice)

    body = email_with_payment_link(invoice, draft_email(invoice, purpose, user_instruction=user_instruction))
    subject = f"{purpose.title()} - {invoice['invoice_number']}"
    priority = invoice_priority(invoice, purpose)
    draft = {
        "id": str(uuid.uuid4()),
        "invoice_id": invoice["id"],
        "invoice_number": invoice.get("invoice_number", ""),
        "po_number": invoice.get("po_number", ""),
        "purpose": purpose,
        "dedupe_key": key,
        "priority": priority["code"],
        "priority_label": priority["label"],
        "priority_reason": priority["reason"],
        "client_name": invoice["client_name"],
        "client_email": invoice.get("client_email", ""),
        "subject": subject,
        "body": body,
        "payment_link": invoice.get("stripe_payment_link") or "Stripe payment link pending",
        "source": source,
        "origin_label": origin_label(source),
        "attachment_url": invoice.get("pdf_path", ""),
        "attachment_name": f"{invoice_pdf_file_stem(invoice)}.pdf" if invoice.get("pdf_path") else "",
        "status": "awaiting_confirmation",
        "created_at": now_iso(),
        "sent_at": "",
    }
    state.setdefault("drafts", []).insert(0, draft)
    if not reasoning:
        reasoning = f"{invoice['invoice_number']} meets the conditions for {purpose}; human approval is required before sending."
    if log_activity:
        add_activity(
            state,
            f"Drafted {purpose} for {invoice['client_name']} - awaiting confirmation.",
            source=source,
            kind="Draft Created",
            reasoning=reasoning,
            outcome="Placed in Waiting for your OK; no email has been sent automatically.",
        )
    return draft


def create_action_approval(
    state: dict[str, Any],
    *,
    approval_type: str,
    subject: str,
    body: str,
    client_name: str = "",
    client_email: str = "",
    command: str = "",
    payload: dict[str, Any] | None = None,
    reasoning: str = "",
    provider: str = "",
    action_summary: str = "",
) -> dict[str, Any]:
    payload = payload or {}
    key = f"{approval_type}:{client_name}:{command}:{json.dumps(payload, sort_keys=True, ensure_ascii=False)}"
    for draft in state.setdefault("drafts", []):
        if draft.get("dedupe_key") == key and draft.get("status") == "awaiting_confirmation":
            return draft
        if draft.get("dedupe_key") == key and draft.get("status") == "held" and held_until_is_future(draft):
            return draft
    priority = approval_priority(approval_type, payload)
    draft = {
        "id": str(uuid.uuid4()),
        "approval_type": approval_type,
        "invoice_id": payload.get("invoice_id", ""),
        "purpose": approval_type.replace("_", " "),
        "dedupe_key": key,
        "priority": priority["code"],
        "priority_label": priority["label"],
        "priority_reason": priority["reason"],
        "client_name": client_name,
        "client_email": client_email,
        "subject": subject,
        "body": body,
        "source": "User Command" if command else "Lynn",
        "origin_label": origin_label("User Command", command),
        "attachment_url": "",
        "attachment_name": "",
        "status": "awaiting_confirmation",
        "created_at": now_iso(),
        "sent_at": "",
        "command": command,
        "action_payload": payload,
        "reasoning": reasoning,
        "reasoning_summary": concise_reasoning(reasoning),
        "provider": provider or payload.get("provider", ""),
        "action_summary": action_summary or body.split("\n", 1)[0],
    }
    state.setdefault("drafts", []).insert(0, draft)
    add_activity(
        state,
        f"Pending approval created: {subject}.",
        source="User Command",
        kind="Recommended Action",
        reasoning=reasoning or "The Ask Lynn request produced an action that requires your OK.",
        outcome="Placed in Waiting for your OK; no external action was executed.",
    )
    return draft


def invoice_pdf_file_stem(invoice: dict[str, Any]) -> str:
    parts = [
        str(invoice.get("invoice_number") or "invoice"),
        str(invoice.get("po_number") or ""),
    ]
    raw = "--".join(part for part in parts if part)
    return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)


def generate_invoice_pdf(invoice: dict[str, Any]) -> str:
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    safe_name = invoice_pdf_file_stem(invoice)
    path = GENERATED_DIR / f"{safe_name}.pdf"
    doc = SimpleDocTemplate(
        str(path),
        pagesize=letter,
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.75 * inch,
        bottomMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    green = colors.HexColor("#0f8a5f")
    dark = colors.HexColor("#172033")
    muted = colors.HexColor("#64748b")
    light_green = colors.HexColor("#e8f7ef")
    line = colors.HexColor("#cbd5e1")
    amount_text = f"{invoice.get('currency', 'USD')} {float(invoice.get('amount', 0)):,.2f}"
    payment_link = invoice.get("stripe_payment_link") or "Payment link pending"

    logo_text = Paragraph(
        "<font size='26' color='#0f8a5f'><b>HelloBike</b></font><br/>"
        "<font size='9' color='#64748b'>E-Bike Factory & Export Manufacturing</font>",
        styles["Normal"],
    )
    logo = Table([[BikeLogo(), logo_text]], colWidths=[1.05 * inch, 2.75 * inch])
    logo.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    company = Paragraph(
        "<b>HelloBike Manufacturing Co.</b><br/>"
        "No. 88 Green Mobility Road<br/>"
        "Shenzhen, Guangdong, China<br/>"
        "finance@hellobike-factory.example",
        styles["Normal"],
    )
    header = Table([[logo, company]], colWidths=[3.9 * inch, 2.7 * inch])
    header.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("LINEBELOW", (0, 0), (-1, -1), 1.2, green),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 14),
            ]
        )
    )

    title = Table(
        [
            [
                Paragraph("<font size='22'><b>Commercial Invoice</b></font>", styles["Normal"]),
                Paragraph(f"<font size='11' color='#64748b'>Invoice No.</font><br/><font size='15'><b>{invoice['invoice_number']}</b></font>", styles["Normal"]),
            ]
        ],
        colWidths=[4.1 * inch, 2.5 * inch],
    )
    title.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                ("TOPPADDING", (0, 0), (-1, -1), 16),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )

    bill_to = Paragraph(
        f"<font color='#64748b'>Bill To</font><br/>"
        f"<font size='13'><b>{invoice.get('client_name', '')}</b></font><br/>"
        f"{invoice.get('client_email', '')}",
        styles["Normal"],
    )
    invoice_meta = Table(
        [
            ["PO Number", invoice.get("po_number", "")],
            ["Invoice Date", invoice.get("invoice_date", "")],
            ["Due Date", invoice.get("due_date", "")],
            ["Payment Terms", invoice.get("payment_terms", "")],
        ],
        colWidths=[1.25 * inch, 1.75 * inch],
    )
    invoice_meta.setStyle(
        TableStyle(
            [
                ("TEXTCOLOR", (0, 0), (0, -1), muted),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    details = Table([[bill_to, invoice_meta]], colWidths=[3.4 * inch, 3.2 * inch])
    details.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("BACKGROUND", (0, 0), (0, 0), light_green),
                ("BOX", (0, 0), (0, 0), 0.6, colors.HexColor("#b7dfc8")),
                ("BOX", (1, 0), (1, 0), 0.6, line),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )

    item_rows = [
        ["Description", "Qty", "Unit Price", "Amount"],
        [f"E-bike export order - {invoice.get('notes') or invoice.get('po_number')}", "1", amount_text, amount_text],
    ]
    items = Table(item_rows, colWidths=[3.7 * inch, 0.7 * inch, 1.2 * inch, 1.0 * inch])
    items.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), dark),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                ("GRID", (0, 0), (-1, -1), 0.5, line),
                ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
            ]
        )
    )

    total = Table(
        [
            ["Subtotal", amount_text],
            ["Amount Due", amount_text],
        ],
        colWidths=[1.4 * inch, 1.35 * inch],
    )
    total.setStyle(
        TableStyle(
            [
                ("ALIGN", (0, 0), (-1, -1), "RIGHT"),
                ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
                ("TEXTCOLOR", (0, 1), (-1, 1), green),
                ("LINEABOVE", (0, 1), (-1, 1), 1, green),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    total_wrap = Table([["", total]], colWidths=[3.85 * inch, 2.75 * inch])
    total_wrap.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP")]))

    payment = Table(
        [[Paragraph(f"<b>Payment</b><br/>{payment_link}", styles["Normal"])]],
        colWidths=[6.6 * inch],
    )
    payment.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#f8fafc")),
                ("BOX", (0, 0), (-1, -1), 0.6, line),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 12),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )

    story = [
        header,
        title,
        details,
        Spacer(1, 0.25 * inch),
        items,
        Spacer(1, 0.15 * inch),
        total_wrap,
        Spacer(1, 0.2 * inch),
        payment,
        Spacer(1, 0.28 * inch),
        Paragraph("<font color='#64748b'>Thank you for choosing HelloBike. Built for clean mobility, exported with care.</font>", styles["Normal"]),
    ]
    doc.build(story)
    invoice["pdf_path"] = f"/generated/{path.name}"
    return invoice["pdf_path"]


def verify_stripe_signature(payload: bytes, header: str) -> bool:
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET")
    if not secret:
        return True
    parts = dict(item.split("=", 1) for item in header.split(",") if "=" in item)
    timestamp = parts.get("t")
    signature = parts.get("v1")
    if not timestamp or not signature:
        return False
    signed_payload = f"{timestamp}.".encode("utf-8") + payload
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def demo_invoices() -> list[dict[str, Any]]:
    rows = [
        ("PO-2026-001", "Hannoosh Ltd", "ganesh@hannoosh.com", 12500, "Net 30", dt.date(2026, 5, 1), dt.date(2026, 5, 3), "Machine parts batch 1"),
        ("PO-2026-002", "Innaworks", "pavan@innaworks.com", 8200, "Net 30", dt.date(2026, 5, 8), dt.date(2026, 5, 10), "Electronics components"),
        ("PO-2026-003", "PLU-Pluto", "amy@pluto.com", 6000, "Net 60", dt.date(2026, 4, 28), dt.date(2026, 5, 1), "Packaging materials"),
        ("PO-2026-004", "Hannoosh Ltd", "ganesh@hannoosh.com", 3500, "Net 30", dt.date(2026, 4, 8), dt.date(2026, 4, 10), "Machine parts batch 2"),
        ("PO-2026-005", "Innaworks", "pavan@innaworks.com", 15000, "Net 60", dt.date(2026, 4, 12), dt.date(2026, 4, 15), "Bulk order Q2"),
        ("PO-2026-006", "PLU-Pluto", "amy@pluto.com", 4800, "Net 30", dt.date(2026, 6, 1), dt.date(2026, 6, 5), "Awaiting shipment confirmation"),
        ("PO-2026-007", "Hannoosh Ltd", "ganesh@hannoosh.com", 9200, "Net 30", dt.date(2026, 5, 18), dt.date(2026, 5, 20), "Special order"),
        ("PO-2026-008", "Innaworks", "pavan@innaworks.com", 22000, "Net 90", dt.date(2026, 2, 28), dt.date(2026, 3, 1), "Annual contract Q1"),
    ]
    invoices = []
    for index, (po, client, email, amount, terms, inv_date, shipment_date, notes) in enumerate(rows):
        due = due_date_for(inv_date, terms)
        invoices.append(
            {
                "id": str(uuid.uuid4()),
                "invoice_number": f"INV-2026-{index + 1:03d}",
                "po_number": po,
                "client_name": client,
                "client_email": email,
                "amount": amount,
                "currency": "USD",
                "payment_terms": terms,
                "invoice_date": inv_date.isoformat(),
                "due_date": due.isoformat(),
                "shipment_date": shipment_date.isoformat(),
                "notes": notes,
                "status": status_for_due(due),
                "stripe_payment_link": "",
                "stripe_payment_link_id": "",
                "pdf_path": "",
                "risk_profile": None,
                "payment_details": None,
                "created_at": now_iso(),
                "paid_at": "",
            }
        )
    return invoices


def demo_pos() -> list[dict[str, Any]]:
    pos = []
    for index, invoice in enumerate(demo_invoices()):
        pos.append(
            {
                "id": str(uuid.uuid4()),
                "po_number": invoice["po_number"],
                "proposed_invoice_number": invoice["invoice_number"],
                "client_name": invoice["client_name"],
                "client_email": invoice["client_email"],
                "amount": invoice["amount"],
                "currency": invoice["currency"],
                "payment_terms": invoice["payment_terms"],
                "po_date": invoice["invoice_date"],
                "shipment_date": invoice["shipment_date"],
                "notes": invoice["notes"],
                "status": "ready_to_invoice",
                "status_reason": "Shipment date is complete or within 10 days; Lynn can prepare the invoice for your OK.",
                "created_at": invoice["created_at"],
                "approved_at": "",
                "invoice_id": "",
            }
        )
    return pos


class App(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_HEAD(self) -> None:
        path = parse.urlparse(self.path).path
        if path in {"", "/"}:
            file_path = STATIC_DIR / "landing.html"
        elif path == "/app":
            file_path = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            file_path = STATIC_DIR / path.replace("/static/", "", 1)
        elif path.startswith("/generated/"):
            file_path = GENERATED_DIR / path.replace("/generated/", "", 1)
        else:
            file_path = STATIC_DIR / path.lstrip("/")
        if file_path.exists():
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(file_path.name)[0] or "application/octet-stream")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_GET(self) -> None:
        path = parse.urlparse(self.path).path
        if path == "/api/state":
            state = read_state()
            json_response(self, state_payload(state))
            return
        if path == "/api/config":
            json_response(self, config_status())
            return
        self.serve_file(path)

    def do_POST(self) -> None:
        path = parse.urlparse(self.path).path
        try:
            if path == "/api/reset":
                state = default_state()
                state["po_pipeline"] = demo_pos()
                state["invoices"] = []
                state["command_results"] = []
                summary = po_parse_summary(state["po_pipeline"])
                approval_count = create_invoice_approvals_for_ready_pos(state, source="PO Parser", log_activity=False)
                add_activity(
                    state,
                    f"Found {summary['total']} demo POs. {summary['ready']} are ready to invoice based on shipment date.",
                    source="PO Parser",
                    kind="File Reasoning",
                    reasoning=(
                        f"The agent checked shipment dates and client emails. {summary['missing_email']} POs are missing client email; "
                        f"{summary['future_shipment']} have future or unknown shipment dates."
                    ),
                    outcome=f"{approval_count} ready PO approval item{'' if approval_count == 1 else 's'} added to Waiting for your OK. No invoice or customer email was sent.",
                )
                write_state(state)
                json_response(self, state_payload(state, ok=True))
                return
            if path == "/api/upload":
                self.handle_upload()
                return
            if path == "/api/daily-run":
                self.handle_daily_run()
                return
            if path == "/api/scheduler":
                self.handle_scheduler()
                return
            if path == "/api/command":
                self.handle_command()
                return
            if path == "/api/stripe/webhook":
                self.handle_stripe_webhook()
                return
            if path == "/api/stripe/backfill-links":
                self.handle_stripe_backfill_links()
                return
            if path == "/api/stripe/sync-payments":
                self.handle_stripe_sync_payments()
                return

            invoice_action = re.match(r"^/api/invoices/([^/]+)/([^/]+)$", path)
            if invoice_action:
                self.handle_invoice_action(invoice_action.group(1), invoice_action.group(2))
                return

            draft_action = re.match(r"^/api/drafts/([^/]+)/([^/]+)$", path)
            if draft_action:
                self.handle_draft_action(draft_action.group(1), draft_action.group(2))
                return

            command_action = re.match(r"^/api/commands/([^/]+)/dismiss$", path)
            if command_action:
                self.handle_command_dismiss(command_action.group(1))
                return

            command_followup = re.match(r"^/api/commands/([^/]+)/draft-followup$", path)
            if command_followup:
                self.handle_command_draft_followup(command_followup.group(1))
                return

            json_response(self, {"error": "Not found"}, 404)
        except Exception as exc:
            json_response(self, {"error": str(exc)}, 500)

    def serve_file(self, path: str) -> None:
        if path in {"", "/"}:
            file_path = STATIC_DIR / "landing.html"
        elif path == "/app":
            file_path = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            file_path = STATIC_DIR / path.replace("/static/", "", 1)
        elif path.startswith("/generated/"):
            file_path = GENERATED_DIR / path.replace("/generated/", "", 1)
        else:
            file_path = STATIC_DIR / path.lstrip("/")

        file_path = file_path.resolve()
        allowed = [STATIC_DIR.resolve(), GENERATED_DIR.resolve()]
        if not any(str(file_path).startswith(str(root)) for root in allowed) or not file_path.exists():
            text_response(self, "Not found", 404)
            return
        raw = file_path.read_bytes()
        content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_upload(self) -> None:
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type", "")},
        )
        file_item = form["file"] if "file" in form else None
        if file_item is None or not getattr(file_item, "filename", ""):
            json_response(self, {"error": "Missing uploaded file"}, 400)
            return
        content = file_item.file.read()
        df = load_dataframe(file_item.filename, content)
        df = df.dropna(how="all")
        columns = [str(c) for c in df.columns]
        samples = df.head(5).fillna("").to_dict(orient="records")
        mapping_source = "heuristic"
        try:
            mapping = ai_column_mapping(columns, samples) if llm_configured() else heuristic_mapping(columns)
            mapping_source = llm_provider() if llm_configured() else "heuristic"
        except Exception:
            mapping = heuristic_mapping(columns)
            mapping_source = "heuristic_fallback"

        pos = [build_po_record(row, mapping, index) for index, row in enumerate(df.fillna("").to_dict(orient="records"))]
        state = read_state()
        merge_summary = merge_uploaded_pos(state, pos)
        summary = po_parse_summary(merge_summary["added"])
        approval_count = create_invoice_approvals_for_ready_pos(state, source="PO Parser", log_activity=False)
        duplicate_note = (
            f" {merge_summary['duplicate_count']} duplicate POs were skipped."
            if merge_summary["duplicate_count"]
            else ""
        )
        add_activity(
            state,
            (
                f"Found {merge_summary['uploaded_count']} uploaded POs. "
                f"{merge_summary['added_count']} were added and {summary['ready']} are ready to invoice."
                f"{duplicate_note}"
            ),
            source="PO Parser",
            kind="File Reasoning",
            reasoning=(
                f"Column mapping used {mapping_source}. The agent checked shipment dates, payment terms, and client emails. "
                f"{summary['missing_email']} newly added POs are missing client email and should be flagged before sending. "
                "Duplicate detection uses PO Number first, then client, amount, shipment date, and payment terms when PO Number is missing."
            ),
            outcome=(
                f"{approval_count} ready PO approval item{'' if approval_count == 1 else 's'} added to Waiting for your OK. "
                "Duplicate POs were not displayed again. No invoice or customer email was sent."
            ),
        )
        write_state(state)
        json_response(
            self,
            state_payload(
                state,
                ok=True,
                mapping=mapping,
                uploadSummary={
                    **merge_summary,
                    "added": [po.get("po_number", "") for po in merge_summary["added"]],
                    "duplicates": [po.get("po_number", "") for po in merge_summary["duplicates"]],
                },
            ),
        )

    def handle_daily_run(self) -> None:
        body = read_json_body(self) if self.headers.get("Content-Length") else {}
        source = body.get("source") or "Manual Run"
        state = read_state()
        display_source = "Morning check-in"
        ready_approvals = refresh_po_pipeline(state, source=display_source, log_activity=False)
        active = [i for i in state.get("invoices", []) if i.get("status") != "paid"]
        previous_outstanding = state.get("scheduler", {}).get("last_outstanding")
        previous_status = {item.get("invoice_number"): item.get("status") for item in active}
        overdue_drafts = 0
        pre_due_drafts = 0
        partial_drafts = 0
        partial_exceptions = 0
        exa_risk_updates = 0
        exa_risk_errors = 0
        no_change = 0
        paid_count = len([i for i in state.get("invoices", []) if i.get("status") == "paid"])
        run_metrics = invoice_metrics(state.get("invoices", []))
        for invoice in active:
            due = parse_date(invoice.get("due_date"))
            if not due:
                continue
            days = (due - today()).days
            payment = invoice.get("payment_details") or {}
            remaining = float(payment.get("remaining_amount") or 0)
            is_partial = invoice.get("status") == "partial_paid" or remaining > 0
            if os.environ.get("EXA_API_KEY") and days <= 7:
                try:
                    update_invoice_risk(invoice)
                    exa_risk_updates += 1
                except Exception:
                    exa_risk_errors += 1
            if not is_partial:
                risk_score = int((invoice.get("risk_profile") or {}).get("score", 0))
                invoice["status"] = status_for_due(due, risk_score=risk_score)
            if previous_status.get(invoice.get("invoice_number")) == invoice["status"]:
                no_change += 1
            detect_invoice_anomalies(state, invoice, run_metrics, source=display_source)
            if is_partial:
                partial_exceptions += 1
                purpose = "partial payment follow-up"
                ensure_agent_invoice_artifacts(state, invoice, source=display_source, purpose=purpose, log_activity=False)
                before = len(state.setdefault("drafts", []))
                create_draft(
                    state,
                    invoice,
                    purpose,
                    source=display_source,
                    reasoning=invoice_decision_reason(invoice, purpose),
                    log_activity=False,
                )
                if len(state.get("drafts", [])) > before:
                    partial_drafts += 1
                record_anomaly(
                    state,
                    invoice,
                    "Partial payment",
                    source=display_source,
                    reasoning=invoice_decision_reason(invoice, purpose),
                    action=f"Prepared a balance follow-up for the remaining {invoice.get('currency', 'USD')} {remaining:,.2f}; waiting for your OK.",
                )
                continue
            if days < 0:
                purpose = overdue_followup_purpose(abs(days))
                ensure_agent_invoice_artifacts(state, invoice, source=display_source, purpose=purpose, log_activity=False)
                before = len(state.setdefault("drafts", []))
                create_draft(
                    state,
                    invoice,
                    purpose,
                    source=display_source,
                    reasoning=invoice_decision_reason(invoice, purpose),
                    log_activity=False,
                )
                if len(state.get("drafts", [])) > before:
                    overdue_drafts += 1
            elif days <= 7:
                lead_days = pre_due_lead_days(invoice.get("payment_terms", "Net 30"))
                if days <= lead_days:
                    ensure_agent_invoice_artifacts(state, invoice, source=display_source, purpose="pre-due reminder", log_activity=False)
                    before = len(state.setdefault("drafts", []))
                    create_draft(
                        state,
                        invoice,
                        "pre-due reminder",
                        source=display_source,
                        reasoning=invoice_decision_reason(invoice, "pre-due reminder"),
                        log_activity=False,
                    )
                    if len(state.get("drafts", [])) > before:
                        pre_due_drafts += 1
            else:
                lead_days = pre_due_lead_days(invoice.get("payment_terms", "Net 30"))
                if days <= lead_days:
                    ensure_agent_invoice_artifacts(state, invoice, source=display_source, purpose="pre-due reminder", log_activity=False)
                    before = len(state.setdefault("drafts", []))
                    create_draft(
                        state,
                        invoice,
                        "pre-due reminder",
                        source=display_source,
                        reasoning=invoice_decision_reason(invoice, "pre-due reminder"),
                        log_activity=False,
                    )
                    if len(state.get("drafts", [])) > before:
                        pre_due_drafts += 1
        metrics = invoice_metrics(state.get("invoices", []))
        delta_text = "baseline recorded"
        if previous_outstanding is not None:
            delta = round(float(previous_outstanding) - float(metrics["outstanding"]), 2)
            delta_text = f"net cash position improved by ${delta:,.2f}" if delta >= 0 else f"net cash position declined by ${abs(delta):,.2f}"
        forecast_90 = forecast_cash_flow(forecast_records_from_state(state), 90)
        state.setdefault("scheduler", default_scheduler())
        state["scheduler"]["last_run_at"] = now_iso()
        state["scheduler"]["last_outstanding"] = metrics["outstanding"]
        state["scheduler"]["last_forecast_90"] = forecast_90
        state["scheduler"]["next_run_at"] = next_scheduled_run_iso(state["scheduler"].get("time", "09:00"))
        message = (
            f"Lynn checked {len(state.get('po_pipeline', []))} POs and {len(active)} active invoices: "
            f"{ready_approvals} PO invoice approval{'' if ready_approvals == 1 else 's'}, "
            f"{pre_due_drafts} pre-due reminder{'' if pre_due_drafts == 1 else 's'}, "
            f"{overdue_drafts} overdue escalation{'' if overdue_drafts == 1 else 's'}, "
            f"{partial_drafts} partial-payment follow-up{'' if partial_drafts == 1 else 's'}, "
            f"{exa_risk_updates} Exa buyer-risk refresh{'' if exa_risk_updates == 1 else 'es'}, "
            "90-day cash forecast updated."
        )
        reasoning = (
            "Daily check scope: ready-to-invoice POs, pre-due reminders by payment terms "
            "(Net 30: 7 days, Net 60: 14 days, Net 90: 30 days), overdue escalation at D+1/D+7/D+30, "
            "Stripe payment mismatches, Exa buyer-risk signals for invoices due within 7 days or already overdue, "
            f"and 90-day cash forecast. Lynn compared status changes and payment records; {delta_text}."
        )
        exa_error_suffix = f", {exa_risk_errors} Exa error{'' if exa_risk_errors == 1 else 's'}" if exa_risk_errors else ""
        outcome = (
            f"{ready_approvals + pre_due_drafts + overdue_drafts + partial_drafts} item"
            f"{'' if ready_approvals + pre_due_drafts + overdue_drafts + partial_drafts == 1 else 's'} placed in Waiting for your OK. "
            f"{paid_count} paid invoice{'' if paid_count == 1 else 's'} recorded; "
            f"{partial_exceptions} partial-payment exception{'' if partial_exceptions == 1 else 's'} checked; "
            f"{exa_risk_updates} Exa risk profile{'' if exa_risk_updates == 1 else 's'} updated"
            f"{exa_error_suffix}."
        )
        add_activity(
            state,
            message,
            source=display_source,
            kind="Morning Check-in",
            reasoning=reasoning,
            outcome=outcome,
        )
        write_state(state)
        json_response(self, state_payload(state, ok=True))

    def handle_scheduler(self) -> None:
        body = read_json_body(self) if self.headers.get("Content-Length") else {}
        state = read_state()
        scheduler = state.setdefault("scheduler", default_scheduler())
        if "enabled" in body:
            scheduler["enabled"] = bool(body.get("enabled"))
        if body.get("time"):
            scheduler["time"] = str(body.get("time"))
        scheduler["timezone"] = "Asia/Shanghai"
        scheduler["mode"] = "Vercel Cron ready"
        scheduler["next_run_at"] = next_scheduled_run_iso(scheduler.get("time", "09:00"))
        add_activity(
            state,
            f"Scheduled AR agent {'enabled' if scheduler['enabled'] else 'paused'} for daily {scheduler['time']} Asia/Shanghai.",
            source="User Action",
            kind="Scheduler Config",
            reasoning="Lynn is configured to run the Morning check-in autonomously; production deployment should connect Vercel Cron to /api/daily-run.",
            outcome="Scheduler settings updated.",
        )
        write_state(state)
        json_response(self, state_payload(state, ok=True))

    def handle_invoice_action(self, invoice_id: str, action: str) -> None:
        body = read_json_body(self) if self.headers.get("Content-Length") else {}
        state = read_state()
        invoice = find_invoice(state, invoice_id)
        if action == "payment-link":
            payment_link = create_stripe_link(invoice)
            update_invoice_drafts_payment_link(state, invoice)
            add_activity(
                state,
                f"Stripe payment link created for {invoice['invoice_number']}.",
                source="User Action",
                kind="Stripe Tool Use",
                reasoning="A human requested a payment link; the agent used Stripe so the customer can pay against this invoice.",
                outcome="Payment link attached to invoice.",
            )
            write_state(state)
            json_response(self, state_payload(state, ok=True, paymentLink=payment_link))
            return
        if action == "pdf":
            pdf_path = generate_invoice_pdf(invoice)
            invoice["pdf_path"] = pdf_path
            add_activity(
                state,
                f"Invoice PDF generated for {invoice['invoice_number']}.",
                source="User Action",
                kind="Invoice Generation",
                reasoning="A human requested the customer-visible invoice PDF; internal risk notes are excluded from the PDF.",
                outcome="HelloBike invoice PDF generated.",
            )
            write_state(state)
            json_response(self, state_payload(state, ok=True, pdfPath=pdf_path))
            return
        if action == "draft-email":
            purpose = body.get("purpose", "invoice email")
            draft = create_draft(
                state,
                invoice,
                purpose,
                source="User Action",
                reasoning=invoice_decision_reason(invoice, purpose),
            )
            write_state(state)
            json_response(self, state_payload(state, ok=True, draft=draft))
            return
        if action in {"risk-check", "risk", "rick-check", "rick"}:
            profile = update_invoice_risk(invoice)
            provider = "Exa" if profile.get("provider") == "exa" else "baseline rules"
            add_activity(
                state,
                f"Risk intelligence updated for {invoice['invoice_number']} using {provider}.",
                source="Risk Intelligence",
                kind="Risk Reasoning",
                reasoning=profile.get("summary", "The agent reviewed invoice age, customer signals, and available external intelligence."),
                outcome=f"Risk score {profile.get('score')}/100; suggested tone: {profile.get('recommended_tone')}.",
            )
            write_state(state)
            json_response(self, state_payload(state, ok=True, riskProfile=profile))
            return
        if action == "simulate-paid":
            payment = mark_invoice_paid(
                invoice,
                method="card",
                reference=f"plink_demo_{invoice['invoice_number'].lower()}",
                amount=float(invoice.get("amount", 0)),
                currency=invoice.get("currency", "USD"),
            )
            closed_drafts = close_pending_drafts_for_paid_invoice(state, invoice)
            add_activity(
                state,
                f"Payment of ${payment['amount']:,.2f} received from {invoice['client_name']} via Stripe.",
                source="Stripe Payment",
                kind="Payment Reasoning",
                reasoning="The agent matched the Stripe payment to the invoice and compared received amount with invoice amount.",
                outcome=(
                    f"{invoice['invoice_number']} marked paid; {closed_drafts} pending follow-up item{'' if closed_drafts == 1 else 's'} closed."
                    if payment.get("remaining_amount", 0) == 0
                    else f"Partial payment flagged; ${payment['remaining_amount']:,.2f} remains outstanding."
                ),
            )
            write_state(state)
            json_response(self, state_payload(state, ok=True))
            return
        json_response(self, {"error": "Unknown invoice action"}, 404)

    def handle_stripe_backfill_links(self) -> None:
        state = read_state()
        result = backfill_create_invoice_payment_links(state)
        write_state(state)
        json_response(self, state_payload(state, ok=True, stripeBackfill=result))

    def handle_stripe_sync_payments(self) -> None:
        state = read_state()
        if not os.environ.get("STRIPE_SECRET_KEY"):
            json_response(self, state_payload(state, ok=True, stripeSync={"checked": 0, "applied": 0, "errors": []}))
            return
        result = sync_stripe_payments(state)
        write_state(state)
        json_response(self, state_payload(state, ok=True, stripeSync=result))

    def handle_command(self) -> None:
        body = read_json_body(self)
        command = str(body.get("command", "")).strip()
        if not command:
            json_response(self, {"error": "Missing command"}, 400)
            return

        state = read_state()
        normalized = command.lower()
        metrics = invoice_metrics(state.get("invoices", []))
        result: dict[str, Any]

        if "set daily check" in normalized or ("daily check" in normalized and ("am" in normalized or "pm" in normalized)):
            new_time = parse_scheduler_time(command)
            if not new_time:
                message = "I could not find a valid daily check time."
                result = add_command_result(state, command, message, "agent_setting", ["Try: set daily check to 8am."])
            else:
                scheduler = state.setdefault("scheduler", default_scheduler())
                scheduler["time"] = new_time
                scheduler["next_run_at"] = next_scheduled_run_iso(new_time)
                message = f"Daily autonomous check updated to {new_time} Asia/Shanghai."
                add_activity(
                    state,
                    message,
                    source="User Command",
                    kind="Settings Update",
                    reasoning="The user changed the schedule for the autonomous AR check.",
                    outcome="Scheduler updated; Vercel Cron should be configured to match this time in production.",
                )
                result = add_command_result(state, command, message, "agent_setting", [f"Next run: {scheduler['next_run_at']}"], {"setting": "daily_check", "value": new_time})
        elif "payment terms" in normalized and ("change" in normalized or "set" in normalized):
            client = find_client_name(state, command)
            terms = parse_payment_terms(command)
            if not client or not terms:
                message = "I need a client name and Net terms to update payment policy."
                result = add_command_result(state, command, message, "agent_setting", ["Try: Change Hannoosh Ltd payment terms to Net 15 from next PO."])
            else:
                summary = client_receivable_summary(state, client)
                ai = llm_business_summary(
                    command,
                    "Explain whether this future payment-terms policy change should be approved.",
                    {"client": client, "new_terms": terms, "current_summary": summary},
                    f"Approve changing {client} to {terms} from the next PO.",
                    f"{client} has {summary['active_invoices']} active invoices and {money_text(summary['overdue_total'])} overdue. The policy applies only to future POs.",
                )
                approval = create_action_approval(
                    state,
                    approval_type="policy_change",
                    subject=f"Payment Terms Policy - {client}",
                    body=f"Approve future payment terms change for {client}: {terms} from the next PO.\n\nReasoning: {ai['reasoning']}",
                    client_name=client,
                    command=command,
                    payload={"client": client, "payment_terms_next_po": terms, "provider": ai.get("provider", "")},
                    reasoning=ai["reasoning"],
                    provider=ai.get("provider", ""),
                    action_summary=f"Change future POs for {client} to {terms}; existing invoices stay unchanged.",
                )
                message = ai["recommendation"]
                add_activity(
                    state,
                    f"Payment terms policy proposed for {client}.",
                    source="User Command",
                    kind="Policy Update",
                    reasoning=ai["reasoning"],
                    outcome="Pending approval created; existing invoices are unchanged.",
                    provider=ai.get("provider", ""),
                )
                result = add_command_result(
                    state,
                    command,
                    message,
                    "agent_judgment",
                    [ai["reasoning"], "Pending approval created; policy is not active until approved."],
                    {
                        **ai,
                        "client": client,
                        "active_invoices": summary["active_invoices"],
                        "overdue_total": summary["overdue_total"],
                        "max_days_overdue": summary["max_days_overdue"],
                        "approval_id": approval["id"],
                    },
                )
        elif "pause" in normalized and ("email" in normalized or "reminder" in normalized):
            client = find_client_name(state, command)
            if not client:
                message = "I could not identify which client to pause."
                result = add_command_result(state, command, message, "agent_action", ["Try: Pause all emails to Hannoosh Ltd."])
            else:
                pending_drafts = len([
                    draft for draft in state.get("drafts", [])
                    if draft.get("client_name") == client and draft.get("status") == "awaiting_confirmation"
                ])
                summary = client_receivable_summary(state, client)
                ai = llm_business_summary(
                    command,
                    "Explain whether pausing automated email follow-up is appropriate.",
                    {"client": client, "pending_drafts": pending_drafts, "summary": summary},
                    f"Pause automated emails to {client}.",
                    f"{client} has {pending_drafts} pending drafts and {summary['active_invoices']} active invoices. Pausing suppresses future automated email drafts until finance resumes follow-up.",
                )
                approval = create_action_approval(
                    state,
                    approval_type="pause_emails",
                    subject=f"Pause Email Automation - {client}",
                    body=f"Approve pausing automated email follow-up for {client}.\n\nReasoning: {ai['reasoning']}",
                    client_name=client,
                    command=command,
                    payload={"client": client, "provider": ai.get("provider", "")},
                    reasoning=ai["reasoning"],
                    provider=ai.get("provider", ""),
                    action_summary=f"Pause future automated email drafts for {client} until finance resumes follow-up.",
                )
                message = ai["recommendation"]
                add_activity(
                    state,
                    f"Pause email automation proposed for {client}.",
                    source="User Command",
                    kind="Action Trigger",
                    reasoning=ai["reasoning"],
                    outcome="Pending approval created; no automation was paused yet.",
                    provider=ai.get("provider", ""),
                )
                result = add_command_result(
                    state,
                    command,
                    message,
                    "agent_judgment",
                    [ai["reasoning"], "Pending approval created; pause is not active until approved."],
                    {
                        **ai,
                        "client": client,
                        "active_invoices": summary["active_invoices"],
                        "overdue_total": summary["overdue_total"],
                        "max_days_overdue": summary["max_days_overdue"],
                        "approval_id": approval["id"],
                    },
                )
        elif is_overdue_notification_command(command):
            invoices = [i for i in state.get("invoices", []) if i.get("status") != "paid" and days_overdue(i) > 0]
            provider = llm_provider() if llm_configured() else "rules"
            affected = command_chase_invoices(
                state,
                invoices,
                "User Command",
                "overdue notice",
                user_instruction=command,
            )
            message = f"Lynn drafted {len(affected)} overdue notification emails for approval."
            reasoning = "Lynn selected every unpaid invoice with a due date before today and prepared customer email drafts. No emails were sent automatically."
            add_activity(
                state,
                message,
                source="User Command",
                kind="Action Trigger",
                reasoning=reasoning,
                outcome="Drafts are in Waiting for your OK; no email sent until approval.",
                provider=provider,
            )
            result = add_command_result(
                state,
                command,
                message,
                "agent_action",
                ["Review the email drafts below or in Waiting for your OK."],
                {"items": affected, "drafts": affected, "reasoning": reasoning, "provider": provider},
            )
        elif "reminder" in normalized and ("due this week" in normalized or "everyone due" in normalized):
            invoices = [
                i
                for i in state.get("invoices", [])
                if i.get("status") != "paid"
                and parse_date(i.get("due_date"))
                and 0 <= (parse_date(i.get("due_date")) - today()).days <= 7
            ]
            affected = command_chase_invoices(
                state,
                invoices,
                "User Command",
                "pre-due reminder",
                user_instruction=command,
            )
            message = f"Prepared reminders for {len(affected)} invoices due this week."
            ai = llm_business_summary(
                command,
                "Explain this pre-due reminder action. Customer emails are only drafted and require approval.",
                {"affected": affected},
                message,
                "The system selected unpaid invoices due this week and created reminder drafts for human approval; no emails were sent automatically.",
            )
            add_activity(
                state,
                message,
                source="User Command",
                kind="Action Trigger",
                reasoning=ai["reasoning"],
                outcome="Drafts are in Waiting for your OK; no email sent until approval.",
                provider=ai.get("provider", ""),
            )
            result = add_command_result(state, command, ai["recommendation"], "agent_action", ["Review drafts in Waiting for your OK to send."], {"items": affected, **ai})
        elif is_due_window_query(command):
            horizon_days = command_window_days(command)
            payload = due_window_payload(state, horizon_days)
            if payload["items"]:
                names = ", ".join(
                    f"{item['client']} ({item['invoice']}, {money_text(float(item['amount']), item.get('currency', 'USD'))}, due {item['due_date']})"
                    for item in payload["items"]
                )
                message = f"Due in the next {horizon_days} days: {names}."
                details = [
                    f"{item['client']} · {item['invoice']} · {money_text(float(item['amount']), item.get('currency', 'USD'))} · due {item['due_date']}"
                    for item in payload["items"]
                ]
            else:
                message = f"No unpaid invoices are due in the next {horizon_days} days."
                details = ["No customer-facing action was requested or drafted."]
            add_activity(
                state,
                message,
                source="User Command",
                kind="Invoice Due Query",
                reasoning="Lynn treated this as a read-only due-date question and checked unpaid invoice due dates.",
                outcome="Due-date answer shown; no draft or email was created.",
            )
            result = add_command_result(state, command, message, "invoice_due_query", details, payload)
        elif "discount" in normalized or "write it off" in normalized or "worth chasing" in normalized:
            client = find_client_name(state, command)
            if not client:
                if "discount" in normalized:
                    judgment = discount_recommendation_for_clients(state, command)
                    approval = create_action_approval(
                        state,
                        approval_type="discount_offer",
                        subject=f"Early Payment Discount - {judgment['client']}",
                        body=f"Recommended action: {judgment['recommendation']}",
                        client_name=judgment["client"],
                        command=command,
                        payload={"client": judgment["client"], "action": "draft_discount_offer", "provider": judgment.get("provider", "")},
                        reasoning=judgment["reasoning"],
                        provider=judgment.get("provider", ""),
                        action_summary=f"Draft an early-payment discount offer for {judgment['client']} only after your OK.",
                    )
                    message = judgment["recommendation"]
                    add_activity(
                        state,
                        f"Discount candidate judgment prepared for {judgment['client']}.",
                        source="User Command",
                        kind="Agent Judgment",
                        reasoning=judgment["reasoning"],
                        outcome=judgment["recommendation"],
                        provider=judgment.get("provider", ""),
                    )
                    result = add_command_result(state, command, message, "agent_judgment", [judgment["reasoning"], "Pending approval created for the recommended discount action."], {**judgment, "approval_id": approval["id"]})
                else:
                    message = "I need a client name to make that judgment."
                    result = add_command_result(state, command, message, "agent_judgment", ["Try: Is PLU-Pluto worth chasing or should I write it off?"])
            else:
                judgment = judgment_for_client(state, client, command)
                approval = None
                if "discount" in normalized:
                    approval = create_action_approval(
                        state,
                        approval_type="discount_offer",
                        subject=f"Early Payment Discount - {client}",
                        body=f"Recommended action: {judgment['recommendation']}",
                        client_name=client,
                        command=command,
                        payload={"client": client, "action": "draft_discount_offer", "provider": judgment.get("provider", "")},
                        reasoning=judgment["reasoning"],
                        provider=judgment.get("provider", ""),
                        action_summary=f"Draft an early-payment discount offer for {client} only after your OK.",
                    )
                elif "write" in normalized:
                    approval = create_action_approval(
                        state,
                        approval_type="writeoff_review",
                        subject=f"Manual Write-Off Review - {client}",
                        body=f"Recommended action: {judgment['recommendation']}",
                        client_name=client,
                        command=command,
                        payload={"client": client, "action": "manual_writeoff_review", "provider": judgment.get("provider", "")},
                        reasoning=judgment["reasoning"],
                        provider=judgment.get("provider", ""),
                        action_summary=f"Move {client} to manual write-off review only after your OK.",
                    )
                message = judgment["recommendation"]
                add_activity(
                    state,
                    f"Judgment prepared for {client}.",
                    source="User Command",
                    kind="Agent Judgment",
                    reasoning=judgment["reasoning"],
                    outcome=judgment["recommendation"],
                    provider=judgment.get("provider", ""),
                )
                payload = {**judgment}
                if approval:
                    payload["approval_id"] = approval["id"]
                result = add_command_result(state, command, message, "agent_judgment", [judgment["reasoning"]], payload)
        elif "most at risk" in normalized or (
            "risk" in normalized and any(noun in normalized for noun in ["client", "customer", "account", "company"])
        ):
            judgment = risk_recommendation_for_clients(state, command)
            message = judgment["recommendation"]
            add_activity(
                state,
                message,
                source="User Command",
                kind="Risk Query",
                reasoning=judgment["reasoning"],
                outcome="Displayed highest-risk client judgment for review.",
                provider=judgment.get("provider", ""),
            )
            result = add_command_result(state, command, message, "agent_judgment", [judgment["reasoning"]], judgment)
        elif is_overdue_amount_query(command):
            threshold = command_amount_threshold(command)
            invoices = [
                command_invoice_item(i)
                for i in state.get("invoices", [])
                if i.get("status") != "paid" and days_overdue(i) > 0 and float(i.get("amount", 0)) >= threshold
            ]
            message = f"Found {len(invoices)} overdue invoices over {money_text(threshold)}."
            ai = llm_business_summary(
                command,
                "Summarize the matching overdue invoices and recommend prioritization. Use only the supplied invoice list.",
                {"threshold": threshold, "items": invoices},
                message,
                "The system filtered invoices by overdue status and amount threshold. Prioritize the largest and oldest overdue invoices first.",
            )
            add_activity(
                state,
                message,
                source="User Command",
                kind="Invoice Query",
                reasoning=ai["reasoning"],
                outcome="Displayed matching invoices.",
                provider=ai.get("provider", ""),
            )
            result = add_command_result(state, command, ai["recommendation"], "invoice_query", [], {"items": invoices, **ai})
        elif is_cash_flow_command(command) or "collect" in normalized:
            horizon_days = command_window_days(command)
            forecast = forecast_cash_flow(forecast_records_from_state(state), horizon_days)
            message = (
                f"Cash flow forecast through {forecast['end_date']}: "
                f"${forecast['contractual']:,.2f} contractual inflow, "
                f"${forecast['risk_adjusted']:,.2f} risk-adjusted inflow."
            )
            details = [
                f"Window: next {forecast['horizon_days']} days",
                f"At-risk overdue balance included in forecast window: ${forecast['at_risk']:,.2f}",
                "Risk-adjusted forecast weights overdue and high-risk invoices lower instead of treating all receivables as equally collectible.",
            ]
            ai = llm_business_summary(
                command,
                "Summarize this cash-flow forecast. Do not alter any numbers.",
                forecast,
                message,
                "The system calculated contractual and risk-adjusted expected inflows from invoice due dates and risk weights.",
            )
            add_activity(
                state,
                message,
                source="User Command",
                kind="Cash Flow Reasoning",
                reasoning=ai["reasoning"],
                outcome="Rendered a risk-adjusted cash flow forecast.",
                provider=ai.get("provider", ""),
            )
            result = add_command_result(state, command, ai["recommendation"], "cash_flow_forecast", [ai["reasoning"], *details], {**forecast, **ai})
        elif any(word in normalized for word in ["payment", "paid", "cash", "collection", "receivable", "ar status"]):
            payment_payload = payment_status_payload(state, metrics)
            message, details, payment_payload = payment_status_message(command, payment_payload)
            add_activity(
                state,
                message,
                source="User Command",
                kind="Payment Status",
                reasoning="Lynn checked recorded Stripe payment events and invoice payment records. No customer-facing action was requested.",
                outcome="Payment status reported; no emails were drafted or sent.",
                provider="",
            )
            result = add_command_result(state, command, message, "payment_status", details, payment_payload)
        elif "daily" in normalized or "reminder" in normalized or "overdue" in normalized:
            write_state(state)
            self.handle_daily_run()
            return
        elif "risk" in normalized:
            candidates = [i for i in state.get("invoices", []) if i.get("status") != "paid"]
            target_po = None
            if candidates:
                target = max(candidates, key=lambda inv: days_overdue(inv))
                target_scope = "active invoice"
            else:
                ready_pos = [
                    po
                    for po in state.get("po_pipeline", [])
                    if po.get("status") == "ready_to_invoice" and not po.get("invoice_id")
                ]
                target_po = max(ready_pos, key=lambda po: float(po.get("amount", 0) or 0), default=None)
                target = forecast_record_from_po(target_po) if target_po else None
                target_scope = "pending invoice approval"
            if not target:
                message = "No active invoices or ready POs need risk review."
                result = add_command_result(state, command, message, "risk_check", [])
            else:
                profile = update_invoice_risk(target)
                if target_po is not None:
                    target_po["risk_profile"] = profile
                message = (
                    f"Risk check completed for {target['invoice_number']} ({target_scope}): "
                    f"{profile['level']} risk, score {profile['score']}/100."
                )
                ai = llm_business_summary(
                    command,
                    "Summarize this buyer risk check and recommend next internal action.",
                    {
                        "scope": target_scope,
                        "invoice": command_invoice_item(target),
                        "risk_profile": profile,
                        "days_overdue": days_overdue(target),
                    },
                    message,
                    profile.get("summary", "The system calculated risk from due date, invoice age, and available risk profile."),
                )
                details = [
                    f"Scope: {target_scope}",
                    f"Recommended action: {profile.get('recommended_action', 'Standard follow-up')}",
                    f"Suggested tone: {profile.get('recommended_tone', 'Standard')}",
                    f"Source: {'Exa' if profile.get('provider') == 'exa' else 'baseline rules'}",
                ]
                result = add_command_result(
                    state,
                    command,
                    ai["recommendation"],
                    "risk_check",
                    [d for d in details if d],
                    {"invoice": target.get("invoice_number"), "risk_profile": profile, **ai},
                )
            add_activity(
                state,
                message,
                source="User Command",
                kind="Risk Reasoning",
                reasoning=ai["reasoning"] if "ai" in locals() else "The user requested a risk check. The agent selected the most overdue active invoice for review.",
                outcome="Risk note updated for internal use only.",
                provider=ai.get("provider", "") if "ai" in locals() else "",
            )
        else:
            message = "Command understood as a status check. I did not draft or send emails because no explicit action was requested."
            add_activity(
                state,
                message,
                source="User Command",
                kind="Guardrail",
                reasoning="The command did not clearly ask for a customer-facing action, so the agent stayed in read-only status mode.",
                outcome="No draft or email was created.",
            )
            result = add_command_result(state, command, message, "safe_status_check", ["Try: check today's payments, run risk check, or show overdue invoices."])

        write_state(state)
        json_response(
            self,
            state_payload(state, ok=True, commandResult=result),
        )

    def handle_command_dismiss(self, command_id: str) -> None:
        state = read_state()
        for result in state.get("command_results", []):
            if result.get("id") == command_id:
                result["dismissed_at"] = now_iso()
                break
        write_state(state)
        json_response(self, state_payload(state, ok=True))

    def handle_command_draft_followup(self, command_id: str) -> None:
        state = read_state()
        result = next((item for item in state.get("command_results", []) if item.get("id") == command_id), None)
        if not result or result.get("intent") != "risk_check":
            json_response(self, {"error": "Risk check result not found"}, 404)
            return
        payload = result.get("payload") or {}
        invoice_number = str(payload.get("invoice") or "")
        risk_profile = payload.get("risk_profile") if isinstance(payload.get("risk_profile"), dict) else {}
        invoice = next(
            (item for item in state.get("invoices", []) if item.get("invoice_number") == invoice_number and item.get("status") != "paid"),
            None,
        )
        risk_summary = risk_profile.get("summary") or "The latest buyer risk check indicates this customer should be followed up."
        if invoice:
            invoice["risk_profile"] = risk_profile or invoice.get("risk_profile")
            ensure_agent_invoice_artifacts(state, invoice, source="User Command", purpose="buyer risk follow-up", log_activity=False)
            purpose = overdue_followup_purpose(days_overdue(invoice)) if days_overdue(invoice) > 0 else "buyer risk follow-up"
            draft = create_draft(
                state,
                invoice,
                purpose,
                source="User Command",
                reasoning=(
                    f"The user asked Lynn to convert an Exa-assisted risk check into a reviewable follow-up. {risk_summary}"
                ),
                user_instruction="Draft a concise follow-up based on the Exa-assisted buyer risk check.",
            )
            message = f"Drafted follow-up for {invoice.get('invoice_number')} and moved it to Waiting for your OK."
        else:
            po = next(
                (
                    item
                    for item in state.get("po_pipeline", [])
                    if (item.get("proposed_invoice_number") or item.get("po_number")) == invoice_number
                ),
                None,
            )
            if not po:
                json_response(self, {"error": "Invoice or PO not found for this risk result"}, 404)
                return
            po["risk_profile"] = risk_profile or po.get("risk_profile")
            draft = create_invoice_approval(state, po, source="User Command", log_activity=False)
            if risk_profile:
                draft["priority"] = "P1" if int(risk_profile.get("score", 0) or 0) >= 70 else "P2"
                draft["priority_label"] = "Risk review"
                draft["priority_reason"] = f"Exa-assisted buyer risk check: {risk_summary}"
            add_activity(
                state,
                f"Attached buyer risk check to {invoice_number}.",
                source="User Command",
                kind="Risk Routed",
                reasoning="The user chose Draft follow-up for review from an Exa-assisted risk result before an active invoice existed.",
                outcome="Risk note added to the invoice approval already waiting for your OK.",
            )
            message = f"Added Exa risk context to {invoice_number} in Waiting for your OK."
        write_state(state)
        json_response(self, state_payload(state, ok=True, draftedFollowup=draft, message=message))

    def handle_draft_action(self, draft_id: str, action: str) -> None:
        state = read_state()
        draft = next((d for d in state.get("drafts", []) if d.get("id") == draft_id), None)
        if not draft:
            json_response(self, {"error": "Draft not found"}, 404)
            return
        if action == "edit":
            body = read_json_body(self)
            subject = str(body.get("subject") or "").strip()
            draft_body = str(body.get("body") or "").strip()
            if not subject or not draft_body:
                json_response(self, {"error": "Subject and body are required"}, 400)
                return
            draft["subject"] = subject
            draft["body"] = draft_body
            draft["edited_at"] = now_iso()
            add_activity(
                state,
                f"User edited {subject}.",
                source="Human Approval",
                kind="Draft Edited",
                reasoning="The user chose Make changes and updated Lynn's prepared draft before approval.",
                outcome="Draft updated and still waiting for your OK.",
            )
            write_state(state)
            json_response(self, state_payload(state, ok=True))
            return
        if draft.get("approval_type") == "create_invoice":
            po = next((item for item in state.get("po_pipeline", []) if item.get("id") == draft.get("po_id")), None)
            if not po:
                json_response(self, {"error": "PO not found"}, 404)
                return
            if action == "send":
                invoice = invoice_from_po(po)
                if str(draft.get("payment_link", "")).startswith("http"):
                    invoice["stripe_payment_link"] = draft.get("payment_link", "")
                    invoice["stripe_payment_link_id"] = draft.get("payment_link_id", "")
                ensure_agent_invoice_artifacts(state, invoice, source="Human Approval", purpose="invoice email")
                state.setdefault("invoices", []).append(invoice)
                po["status"] = "invoiced"
                po["approved_at"] = now_iso()
                po["invoice_id"] = invoice["id"]
                draft["invoice_id"] = invoice["id"]
                draft["invoice_number"] = invoice.get("invoice_number", "")
                draft["po_number"] = invoice.get("po_number", "")
                draft["attachment_url"] = invoice.get("pdf_path", "")
                draft["attachment_name"] = f"{invoice_pdf_file_stem(invoice)}.pdf" if invoice.get("pdf_path") else ""
                draft["payment_link"] = invoice.get("stripe_payment_link") or "Stripe payment link pending"
                draft["body"] = email_with_payment_link(invoice, draft.get("body") or template_email(invoice, "invoice email"))
                draft["status"] = "sent"
                draft["sent_at"] = now_iso()
                add_activity(
                    state,
                    f"User approved invoice creation for {po['po_number']}.",
                    source="Human Approval",
                    kind="Invoice Created",
                    reasoning="The agent proposed invoice creation because the PO entered the 10-day shipment window.",
                    outcome=f"{invoice['invoice_number']} created and invoice email marked sent.",
                )
            elif action == "skip":
                hold_until = held_until_for_state(state)
                draft["status"] = "held"
                draft["held_at"] = now_iso()
                draft["held_until"] = hold_until
                po["status"] = "held"
                po["held_until"] = hold_until
                add_activity(
                    state,
                    f"User held invoice creation for {po['po_number']}.",
                    source="Human Approval",
                    kind="Invoice Held",
                    reasoning="The user chose Hold for now, so Lynn will wait until the next scheduled 09:00 check before raising it again.",
                    outcome=f"PO hidden from Ready to act on until {hold_until}.",
                )
            else:
                json_response(self, {"error": "Unknown draft action"}, 404)
                return
            write_state(state)
            json_response(self, state_payload(state, ok=True))
            return
        if draft.get("approval_type") in {"policy_change", "pause_emails", "discount_offer", "writeoff_review"}:
            payload = draft.get("action_payload") or {}
            client = payload.get("client") or draft.get("client_name", "")
            if action == "send":
                draft["status"] = "approved"
                draft["sent_at"] = now_iso()
                outcome = "Recommended action approved."
                if draft.get("approval_type") == "policy_change":
                    terms = payload.get("payment_terms_next_po", "")
                    if client and terms:
                        state.setdefault("client_policies", {})[client] = {
                            "payment_terms_next_po": terms,
                            "updated_at": now_iso(),
                        }
                        outcome = f"Future PO policy for {client} set to {terms}; existing invoices unchanged."
                elif draft.get("approval_type") == "pause_emails":
                    paused = state.setdefault("paused_clients", [])
                    if client and client not in paused:
                        paused.append(client)
                    paused_drafts = 0
                    for item in state.get("drafts", []):
                        if item.get("id") != draft.get("id") and item.get("client_name") == client and item.get("status") == "awaiting_confirmation":
                            item["status"] = "paused"
                            paused_drafts += 1
                    outcome = f"Automated email drafting paused for {client}; {paused_drafts} pending drafts paused."
                elif draft.get("approval_type") == "discount_offer":
                    outcome = f"Discount offer action approved for {client}; finance can draft customer terms from this recommendation."
                elif draft.get("approval_type") == "writeoff_review":
                    outcome = f"{client} moved to manual write-off review queue."
                add_activity(
                    state,
                    f"User approved {draft['subject']}.",
                    source="Human Approval",
                    kind="Action Approved",
                    reasoning=draft.get("reasoning", "The user approved the recommended finance action."),
                    outcome=outcome,
                    provider=draft.get("provider") or payload.get("provider", ""),
                )
            elif action == "skip":
                draft["status"] = "held"
                draft["held_at"] = now_iso()
                draft["held_until"] = held_until_for_state(state)
                add_activity(
                    state,
                    f"User held {draft['subject']}.",
                    source="Human Approval",
                    kind="Action Held",
                    reasoning=draft.get("reasoning", "The user chose Hold for now on the recommended action."),
                    outcome="Action moved to Held until the next Morning check-in.",
                    provider=draft.get("provider") or payload.get("provider", ""),
                )
            else:
                json_response(self, {"error": "Unknown draft action"}, 404)
                return
            write_state(state)
            json_response(self, state_payload(state, ok=True))
            return
        if action == "send":
            if draft.get("status") == "sent":
                add_activity(
                    state,
                    f"{draft['subject']} was already sent at {draft.get('sent_at')}.",
                    "warn",
                    source="Human Approval",
                    kind="Duplicate Guard",
                    reasoning="The agent prevents the same approved draft from being sent twice.",
                    outcome="Duplicate send blocked.",
                )
                write_state(state)
                json_response(self, state_payload(state, ok=True, warning="This email was already sent."))
                return
            draft["status"] = "sent"
            draft["sent_at"] = now_iso()
            for invoice in state.get("invoices", []):
                if invoice.get("id") == draft.get("invoice_id"):
                    invoice["last_email_sent_at"] = draft["sent_at"]
                    invoice["last_email_subject"] = draft.get("subject", "")
                    invoice["email_sent_count"] = int(invoice.get("email_sent_count", 0)) + 1
                    break
            add_activity(
                state,
                f"User confirmed send for {draft['subject']}.",
                source="Human Approval",
                kind="Email Sent",
                reasoning="The agent drafted the message, but customer-facing email required human approval before sending.",
                outcome="Draft marked sent and invoice follow-up count updated.",
            )
        elif action == "skip":
            draft["status"] = "held"
            draft["held_at"] = now_iso()
            draft["held_until"] = held_until_for_state(state)
            add_activity(
                state,
                f"User held {draft['subject']}.",
                source="Human Approval",
                kind="Email Held",
                reasoning="The user chose Hold for now, so Lynn will wait until the next scheduled 09:00 check before raising it again.",
                outcome="Draft moved to Held and removed from Waiting for your OK.",
            )
        else:
            json_response(self, {"error": "Unknown draft action"}, 404)
            return
        write_state(state)
        json_response(self, state_payload(state, ok=True))

    def handle_stripe_webhook(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = self.rfile.read(length)
        signature = self.headers.get("Stripe-Signature", "")
        if not verify_stripe_signature(payload, signature):
            json_response(self, {"error": "Invalid Stripe signature"}, 400)
            return
        event = json.loads(payload.decode("utf-8"))
        event_type = event.get("type")
        obj = event.get("data", {}).get("object", {})
        invoice_id = obj.get("metadata", {}).get("invoice_id")
        payment_link_id = obj.get("payment_link")
        if event_type in {"checkout.session.completed", "payment_link.completed"}:
            state = read_state()
            if invoice_id and not payment_link_id:
                invoice = next((item for item in state.get("invoices", []) if item.get("id") == invoice_id), None)
                if invoice:
                    obj["payment_link"] = invoice.get("stripe_payment_link_id", "")
            apply_stripe_checkout_session(state, obj)
            write_state(state)
        json_response(self, {"received": True})


def main() -> None:
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "127.0.0.1")
    server = ThreadingHTTPServer((host, port), App)
    print(f"AR Agent demo running at http://{host}:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
