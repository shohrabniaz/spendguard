"""PayPal client interface. MockPayPalClient runs offline; SandboxPayPalClient
calls the real PayPal sandbox REST API (Orders v2) when credentials are set:
  PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET (from developer.paypal.com > Apps & Credentials, Sandbox)."""
from __future__ import annotations
import base64, json, os, uuid, urllib.request, urllib.parse
from typing import Protocol

class PaymentClient(Protocol):
    def create_order(self, merchant_id: str, amount: str, currency: str, description: str) -> dict: ...
    def get_order(self, order_id: str) -> dict: ...
    def capture_order(self, order_id: str) -> dict: ...

class MockPayPalClient:
    """In-memory stand-in that mirrors the shape of Orders v2 responses."""
    def __init__(self, state_path=None):
        self.state_path = state_path
        self.orders = {}
        if state_path and os.path.exists(state_path):
            with open(state_path) as f:
                self.orders = json.load(f)
    def _save(self):
        if self.state_path:
            os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
            with open(self.state_path, "w") as f:
                json.dump(self.orders, f)
    def get_order(self, order_id):
        return self.orders[order_id]
    def approve_order(self, order_id):
        """Simulate buyer approval for offline tests/demos only."""
        o = self.get_order(order_id)
        if o["status"] != "CREATED":
            raise ValueError("Only CREATED mock orders can be approved")
        o["status"] = "APPROVED"
        self._save()
        return o
    def create_order(self, merchant_id, amount, currency, description):
        oid = "MOCK-" + uuid.uuid4().hex[:12].upper()
        self.orders[oid] = {"id": oid, "status": "CREATED", "purchase_units": [{
            "payee": {"merchant_id": merchant_id},
            "amount": {"currency_code": currency, "value": amount},
            "description": description}]}
        self._save()
        return self.orders[oid]
    def capture_order(self, order_id):
        o = self.orders[order_id]
        if o["status"] != "APPROVED":
            raise ValueError("Buyer approval required before capture")
        o["status"] = "COMPLETED"
        o["purchase_units"][0]["payments"] = {"captures": [{
            "id": "MOCK-CAP-" + uuid.uuid4().hex[:12].upper(), "status": "COMPLETED"}]}
        self._save()
        return o

class SandboxPayPalClient:
    BASE = "https://api-m.sandbox.paypal.com"
    def __init__(self, client_id=None, secret=None, return_url=None, cancel_url=None):
        self.client_id = client_id or os.environ["PAYPAL_CLIENT_ID"]
        self.secret = secret or os.environ["PAYPAL_CLIENT_SECRET"]
        self._token = None
        self.return_url = return_url or os.environ.get("PAYPAL_RETURN_URL", "http://localhost:8765/return")
        self.cancel_url = cancel_url or os.environ.get("PAYPAL_CANCEL_URL", "http://localhost:8765/cancel")
    def _req(self, method, path, body=None, headers=None, form=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            h["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
        r = urllib.request.Request(self.BASE + path, data=data, headers=h, method=method)
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read() or b"{}")
    def _auth(self):
        if not self._token:
            basic = base64.b64encode(f"{self.client_id}:{self.secret}".encode()).decode()
            self._token = self._req("POST", "/v1/oauth2/token", form={"grant_type": "client_credentials"},
                                    headers={"Authorization": "Basic " + basic})["access_token"]
        return {"Authorization": "Bearer " + self._token}
    def create_order(self, merchant_id, amount, currency, description):
        unit = {"amount": {"currency_code": currency, "value": amount}, "description": description[:127]}
        if merchant_id and "@" in merchant_id:
            unit["payee"] = {"email_address": merchant_id}
        elif merchant_id and not merchant_id.startswith("demo-"):
            unit["payee"] = {"merchant_id": merchant_id}
        return self._req("POST", "/v2/checkout/orders", {"intent": "CAPTURE", "purchase_units": [unit],
                         "payment_source": {"paypal": {"experience_context": {
                             "return_url": self.return_url, "cancel_url": self.cancel_url,
                             "user_action": "PAY_NOW"}}}},
                         headers={**self._auth(), "PayPal-Request-Id": str(uuid.uuid4())})
    def get_order(self, order_id):
        return self._req("GET", f"/v2/checkout/orders/{urllib.parse.quote(order_id, safe='')}",
                         headers=self._auth())
    def capture_order(self, order_id):
        # NOTE: in sandbox, capture only succeeds after a buyer approves the order
        # (approve link in create_order response, using a sandbox personal account).
        return self._req("POST", f"/v2/checkout/orders/{urllib.parse.quote(order_id, safe='')}/capture", {},
                         headers={**self._auth(), "PayPal-Request-Id": str(uuid.uuid4())})

def make_client() -> PaymentClient:
    if os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"):
        return SandboxPayPalClient()
    return MockPayPalClient()
