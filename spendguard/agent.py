"""LLM shopping agent. It can only spend money through the `pay` tool, which is
routed through SpendGuard. Uses Groq's OpenAI-compatible chat API (free tier)."""
from __future__ import annotations
import json, os, urllib.request
from .policy import PaymentIntent

TOOL = {"type": "function", "function": {
    "name": "pay",
    "description": "Create a PayPal order awaiting buyer approval; this does not pay the merchant. Amount as a decimal string, e.g. '12.50'.",
    "parameters": {"type": "object", "properties": {
        "merchant_id": {"type": "string"}, "amount": {"type": "string"},
        "currency": {"type": "string"}, "description": {"type": "string"}},
        "required": ["merchant_id", "amount", "currency", "description"]}}}

SYSTEM = ("You are a purchasing agent for a small business. Use the `pay` tool to buy what the user asks for "
          "from the merchant catalogue provided. Pay only what the catalogue lists. After the tool result, "
          "never claim a purchase or payment completed: CREATED means created, awaiting buyer approval. "
          "Only a COMPLETED capture means paid.")

class GroqLLM:
    URL = "https://api.groq.com/openai/v1/chat/completions"
    def __init__(self, model=None, key=None):
        self.model = model or os.environ.get("SPENDGUARD_MODEL", "openai/gpt-oss-120b")
        self.key = key or os.environ["GROQ_API_KEY"]
    def chat(self, messages, tools):
        body = {"model": self.model, "messages": messages, "tools": tools, "temperature": 0}
        r = urllib.request.Request(self.URL, data=json.dumps(body).encode(), method="POST",
                                   headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json",
                                            "User-Agent": "spendguard/0.1"})
        with urllib.request.urlopen(r, timeout=60) as resp:
            return json.loads(resp.read())["choices"][0]["message"]

class ScriptedLLM:
    """Deterministic stand-in for tests/offline demos: emits given tool calls."""
    def __init__(self, calls):
        self.calls, self.i = calls, 0
    def chat(self, messages, tools):
        if self.i < len(self.calls):
            c = self.calls[self.i]; self.i += 1
            return {"role": "assistant", "content": None, "tool_calls": [{
                "id": f"call_{self.i}", "type": "function",
                "function": {"name": "pay", "arguments": json.dumps(c)}}]}
        return {"role": "assistant", "content": "Done."}

def status_text(record):
    """Trusted display text; never use LLM prose as payment confirmation."""
    if record.get("verdict") == "error":
        return "Order request failed; no payment confirmed."
    if record.get("verdict") == "block":
        return "Order blocked; no payment made."
    status = record.get("order_status")
    if status == "CREATED":
        return "Order created, awaiting buyer approval."
    if status == "APPROVED":
        return "Order approved, awaiting capture; not paid yet."
    if (record.get("type") == "capture" and status == "COMPLETED"
            and record.get("capture_statuses")
            and all(s == "COMPLETED" for s in record["capture_statuses"])):
        return "Paid: capture COMPLETED."
    return f"Order status: {status or 'unknown'}; no completed payment confirmed."


def final_text(records):
    return "\n".join(status_text(r) for r in records) or "No order created; no payment confirmed."


def run_agent(llm, guard, user_goal: str, catalogue: str, max_steps=4) -> dict:
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": f"Catalogue:\n{catalogue}\n\nTask: {user_goal}"}]
    records = []
    for _ in range(max_steps):
        msg = llm.chat(messages, [TOOL])
        messages.append({k: v for k, v in msg.items() if k in ("role", "content", "tool_calls")})
        calls = msg.get("tool_calls") or []
        if not calls:
            return {"final": final_text(records), "records": records}
        for c in calls:
            try:
                a = json.loads(c["function"]["arguments"])
                rec = guard.pay(PaymentIntent(a["merchant_id"], str(a["amount"]), a.get("currency", "USD"),
                                              a.get("description", ""), user_goal=user_goal))
                out = {"verdict": rec["verdict"], "order_id": rec["order_id"],
                       "order_status": rec["order_status"], "approve_link": rec.get("approve_link"),
                       "failed_checks": [x["detail"] for x in rec["checks"] if x["status"] != "pass"]}
            except Exception as e:  # malformed tool call
                out = {"verdict": "error", "error": str(e)}
            records.append(out)
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": json.dumps(out)})
    return {"final": final_text(records), "records": records}
