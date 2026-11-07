"""Pure-function policy engine. Every check returns a CheckResult; the payment
is allowed only if no check fails. No I/O here, so it is easy to test."""
from __future__ import annotations
import re
from dataclasses import dataclass, field, asdict
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta, timezone

@dataclass
class PaymentIntent:
    merchant_id: str
    amount: str            # string to avoid float rounding
    currency: str
    description: str = ""
    requested_by: str = "agent"
    user_goal: str = ""    # what the human asked for (for context checks)

@dataclass
class Policy:
    per_txn_limit: Decimal = Decimal("100.00")
    daily_limit: Decimal = Decimal("250.00")
    currency: str = "USD"
    merchant_allowlist: list[str] = field(default_factory=list)
    max_txn_per_hour: int = 5
    anomaly_multiplier: Decimal = Decimal("3")   # amount > k * median of history
    require_human_above: Decimal = Decimal("50.00")

    @classmethod
    def from_dict(cls, d: dict) -> "Policy":
        p = cls()
        for k, v in d.items():
            if not hasattr(p, k):
                raise ValueError(f"unknown policy key: {k}")
            cur = getattr(p, k)
            setattr(p, k, Decimal(str(v)) if isinstance(cur, Decimal) else v)
        return p

@dataclass
class CheckResult:
    name: str
    status: str   # "pass" | "fail" | "review"
    detail: str

@dataclass
class Decision:
    verdict: str  # "allow" | "block" | "review"
    checks: list[CheckResult]
    def to_dict(self):
        return {"verdict": self.verdict, "checks": [asdict(c) for c in self.checks]}

INJECTION_PATTERNS = [
    r"ignore (all|any|the|previous|prior|above) (instructions|rules|policies)",
    r"disregard .{0,30}(instructions|policy|limits?)",
    r"you are now", r"system prompt", r"developer mode",
    r"(raise|increase|bypass|override|disable) .{0,20}(limit|policy|guard|check)",
    r"send (all|the rest|everything)", r"transfer .{0,20}to (this|my|the following) account",
    r"</?(system|assistant|tool)>", r"\bact as\b",
]

def _amount(intent):
    try:
        a = Decimal(intent.amount)
    except (InvalidOperation, TypeError):
        return None
    return a if a.is_finite() else None

def check_amount_format(intent, policy, history):
    a = _amount(intent)
    if a is None or a <= 0 or a.as_tuple().exponent < -2:
        return CheckResult("amount_format", "fail", f"invalid amount {intent.amount!r}")
    return CheckResult("amount_format", "pass", f"{a} ok")

def check_currency(intent, policy, history):
    ok = intent.currency.upper() == policy.currency.upper()
    return CheckResult("currency", "pass" if ok else "fail",
                       f"{intent.currency} {'matches' if ok else 'not allowed; policy is'} {policy.currency}")

def check_per_txn(intent, policy, history):
    a = _amount(intent)
    if a is None:
        return CheckResult("per_txn_limit", "fail", "no valid amount")
    if a > policy.per_txn_limit:
        return CheckResult("per_txn_limit", "fail", f"{a} > limit {policy.per_txn_limit}")
    if a > policy.require_human_above:
        return CheckResult("per_txn_limit", "review", f"{a} > human-approval threshold {policy.require_human_above}")
    return CheckResult("per_txn_limit", "pass", f"{a} <= {policy.per_txn_limit}")

def _recent(history, now, delta):
    out = []
    for h in history:
        if h.get("verdict") != "allow":
            continue
        t = datetime.fromisoformat(h["ts"])
        if now - t <= delta:
            out.append(h)
    return out

def check_daily(intent, policy, history, now=None):
    now = now or datetime.now(timezone.utc)
    a = _amount(intent) or Decimal(0)
    spent = sum((Decimal(h["amount"]) for h in _recent(history, now, timedelta(days=1))), Decimal(0))
    if spent + a > policy.daily_limit:
        return CheckResult("daily_limit", "fail", f"spent {spent} + {a} > {policy.daily_limit}")
    return CheckResult("daily_limit", "pass", f"spent {spent} + {a} <= {policy.daily_limit}")

def check_velocity(intent, policy, history, now=None):
    now = now or datetime.now(timezone.utc)
    n = len(_recent(history, now, timedelta(hours=1)))
    if n >= policy.max_txn_per_hour:
        return CheckResult("velocity", "fail", f"{n} payments in last hour (max {policy.max_txn_per_hour})")
    return CheckResult("velocity", "pass", f"{n} payments in last hour")

def check_allowlist(intent, policy, history):
    if intent.merchant_id in policy.merchant_allowlist:
        return CheckResult("merchant_allowlist", "pass", f"{intent.merchant_id} allowlisted")
    return CheckResult("merchant_allowlist", "fail", f"{intent.merchant_id} not in allowlist")

def check_injection(intent, policy, history):
    text = f"{intent.description}\n{intent.merchant_id}".lower()
    hits = [p for p in INJECTION_PATTERNS if re.search(p, text)]
    if hits:
        return CheckResult("prompt_injection", "fail", f"suspicious text matched {hits}")
    return CheckResult("prompt_injection", "pass", "no injection patterns")

def check_anomaly(intent, policy, history):
    amts = sorted(Decimal(h["amount"]) for h in history if h.get("verdict") == "allow")
    a = _amount(intent)
    if len(amts) < 3 or a is None:
        return CheckResult("amount_anomaly", "pass", "not enough history")
    med = amts[len(amts) // 2]
    if a > med * policy.anomaly_multiplier:
        return CheckResult("amount_anomaly", "review", f"{a} > {policy.anomaly_multiplier}x median {med}")
    return CheckResult("amount_anomaly", "pass", f"{a} within {policy.anomaly_multiplier}x median {med}")

CHECKS = [check_amount_format, check_currency, check_per_txn, check_daily,
          check_velocity, check_allowlist, check_injection, check_anomaly]

def evaluate(intent: PaymentIntent, policy: Policy, history: list[dict]) -> Decision:
    results = [c(intent, policy, history) for c in CHECKS]
    if any(r.status == "fail" for r in results):
        v = "block"
    elif any(r.status == "review" for r in results):
        v = "review"
    else:
        v = "allow"
    return Decision(v, results)
