"""SpendGuard: sits between the agent's tool call and the PayPal client."""
from __future__ import annotations
from .policy import PaymentIntent, Policy, evaluate
from .evidence import EvidenceLog

class SpendGuard:
    def __init__(self, policy: Policy, client, log: EvidenceLog, approver=None):
        self.policy, self.client, self.log = policy, client, log
        self.approver = approver or (lambda intent, decision: False)  # default: deny reviews

    def history(self):
        return [e for e in self.log.entries() if e.get("type") == "payment"]

    def pay(self, intent: PaymentIntent) -> dict:
        decision = evaluate(intent, self.policy, self.history())
        verdict = decision.verdict
        human = None
        if verdict == "review":
            human = bool(self.approver(intent, decision))
            verdict = "allow" if human else "block"
        order = None
        if verdict == "allow":
            order = self.client.create_order(intent.merchant_id, intent.amount, intent.currency, intent.description)
        rec = self.log.append({"type": "payment", "verdict": verdict, "merchant_id": intent.merchant_id,
                               "amount": intent.amount, "currency": intent.currency,
                               "description": intent.description, "user_goal": intent.user_goal,
                               "human_approved": human, "checks": decision.to_dict()["checks"],
                               "order_id": order and order.get("id"), "order_status": order and order.get("status"),
                               "approve_link": next((l["href"] for l in (order or {}).get("links", []) if l.get("rel") in ("approve", "payer-action")), None)})
        return rec

    def capture(self, order_id: str) -> dict:
        """Capture only buyer-approved orders allowed by this evidence log."""
        if not self.log.verify():
            raise ValueError("Evidence chain invalid; refusing capture")
        if not any(r.get("order_id") == order_id and r.get("verdict") == "allow"
                   for r in self.history()):
            raise ValueError("Order has no allowed creation record in this evidence log")
        order = self.client.get_order(order_id)
        event = "already_completed"
        if order.get("status") != "COMPLETED":
            if order.get("status") != "APPROVED":
                raise ValueError("Order is not APPROVED; buyer approval required")
            order = self.client.capture_order(order_id)
            event = "capture"
        captures = [c for u in order.get("purchase_units", [])
                    for c in u.get("payments", {}).get("captures", [])]
        return self.log.append({"type": "capture", "event": event,
                               "order_id": order.get("id"), "order_status": order.get("status"),
                               "capture_ids": [c["id"] for c in captures if c.get("id")],
                               "capture_statuses": [c.get("status") for c in captures]})
