"""Offline-only regression tests. Never call PayPal or Groq."""
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from spendguard.agent import ScriptedLLM, run_agent, status_text
from spendguard.evidence import EvidenceLog
from spendguard.guard import SpendGuard
from spendguard.paypal_client import MockPayPalClient, SandboxPayPalClient
from spendguard.policy import Policy, PaymentIntent
from demo import wait_and_capture, ReturnHandler


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = str(Path(self.tmp.name) / 'events.jsonl')
        self.client = MockPayPalClient()
        self.log = EvidenceLog(self.path)
        policy = Policy.from_dict({'merchant_allowlist': ['shop'], 'per_txn_limit': '100',
                                  'daily_limit': '150', 'require_human_above': '50',
                                  'max_txn_per_hour': 3})
        self.guard = SpendGuard(policy, self.client, self.log)

    def create(self):
        return self.guard.pay(PaymentIntent('shop', '10.00', 'USD', 'paper'))['order_id']

    def test_creation_experience_context(self):
        c = SandboxPayPalClient('fake-id', 'fake-secret')
        with patch.object(c, '_auth', return_value={}), patch.object(c, '_req', return_value={}) as req:
            c.create_order('demo-office', '2.00', 'USD', 'paper')
        ctx = req.call_args.args[2]['payment_source']['paypal']['experience_context']
        self.assertEqual(ctx, {'return_url': 'http://localhost:8765/return',
                               'cancel_url': 'http://localhost:8765/cancel', 'user_action': 'PAY_NOW'})

    def test_custom_return_urls(self):
        c = SandboxPayPalClient('id', 'secret', 'https://example.test/ok', 'https://example.test/no')
        self.assertEqual(c.return_url, 'https://example.test/ok')
        self.assertEqual(c.cancel_url, 'https://example.test/no')

    def test_mock_requires_buyer_approval(self):
        oid = self.create()
        with self.assertRaises(ValueError):
            self.client.capture_order(oid)
        self.assertEqual(self.client.get_order(oid)['status'], 'CREATED')

    def test_guard_rejects_unapproved(self):
        oid = self.create()
        with patch.object(self.client, 'capture_order') as capture:
            with self.assertRaises(ValueError):
                self.guard.capture(oid)
            capture.assert_not_called()

    def test_capture_logged_and_chain_valid(self):
        oid = self.create()
        self.client.approve_order(oid)
        rec = self.guard.capture(oid)
        self.assertEqual(rec['type'], 'capture')
        self.assertEqual(rec['order_status'], 'COMPLETED')
        self.assertTrue(rec['capture_ids'])
        self.assertEqual(status_text(rec), 'Paid: capture COMPLETED.')
        self.assertTrue(self.log.verify())
        self.assertEqual(len(self.guard.history()), 1)  # no double-counted spend

    def test_completed_does_not_recapture(self):
        oid = self.create()
        self.client.approve_order(oid)
        self.guard.capture(oid)
        with patch.object(self.client, 'capture_order') as capture:
            rec = self.guard.capture(oid)
            capture.assert_not_called()
        self.assertEqual(rec['event'], 'already_completed')

    def test_unknown_order_not_captured(self):
        with patch.object(self.client, 'get_order') as get:
            with self.assertRaises(ValueError):
                self.guard.capture('unknown')
            get.assert_not_called()

    def test_corrupt_log_not_captured(self):
        oid = self.create()
        path = Path(self.path)
        path.write_text(path.read_text().replace('paper', 'altered'))
        with self.assertRaises(ValueError):
            self.guard.capture(oid)

    def test_llm_cannot_claim_purchase(self):
        class Liar(ScriptedLLM):
            def chat(self, messages, tools):
                result = super().chat(messages, tools)
                if not result.get('tool_calls'):
                    result['content'] = 'Purchased and paid successfully!'
                return result
        llm = Liar([{'merchant_id': 'shop', 'amount': '10', 'currency': 'USD', 'description': 'paper'}])
        result = run_agent(llm, self.guard, 'buy paper', '')
        self.assertEqual(result['final'], 'Order created, awaiting buyer approval.')
        self.assertEqual(result['records'][0]['order_status'], 'CREATED')

    def test_no_tool_cannot_claim_paid(self):
        result = run_agent(ScriptedLLM([]), self.guard, '', '')
        self.assertEqual(result['final'], 'No order created; no payment confirmed.')

    def test_order_completed_without_capture_not_paid(self):
        self.assertNotIn('Paid:', status_text({'order_status': 'COMPLETED'}))
        self.assertNotIn('Paid:', status_text({'type': 'capture', 'order_status': 'COMPLETED',
                                             'capture_statuses': ['PENDING']}))

    def test_poll_until_approved(self):
        oid = self.create()
        def approve_on_sleep(_):
            self.client.approve_order(oid)
        with patch('demo.time.sleep', side_effect=approve_on_sleep):
            rec = wait_and_capture(self.guard, oid, 10, 1)
        self.assertEqual(rec['order_status'], 'COMPLETED')

    def test_poll_timeout_no_capture(self):
        oid = self.create()
        with patch('demo.time.monotonic', side_effect=[0, 2]), patch.object(self.client, 'capture_order') as capture:
            with self.assertRaises(TimeoutError):
                wait_and_capture(self.guard, oid, 1, 1)
            capture.assert_not_called()

    def test_poll_invalid_interval(self):
        with self.assertRaises(ValueError):
            wait_and_capture(self.guard, 'unused', 1, 0)

    def test_mock_state_persistence(self):
        path = str(Path(self.tmp.name) / 'mock.json')
        c = MockPayPalClient(path)
        oid = c.create_order('shop', '1', 'USD', 'paper')['id']
        c.approve_order(oid)
        self.assertEqual(MockPayPalClient(path).get_order(oid)['status'], 'APPROVED')

    def test_return_cancel_handlers(self):
        for path, expected in [('/return?token=fake', b'not confirmed'), ('/cancel', b'cancelled')]:
            handler = object.__new__(ReturnHandler)
            handler.path = path
            handler.wfile = io.BytesIO()
            handler.send_response = lambda code: self.assertEqual(code, 200)
            handler.send_header = lambda *args: None
            handler.end_headers = lambda: None
            handler.do_GET()
            self.assertIn(expected, handler.wfile.getvalue())
            self.assertNotIn(b'fake', handler.wfile.getvalue())

    def test_offline_cli_create_capture_ignores_credentials(self):
        root = Path(__file__).resolve().parents[1]
        env = {**os.environ, 'PAYPAL_CLIENT_ID': 'FAKE-NOT-A-KEY', 'PAYPAL_CLIENT_SECRET': 'FAKE-NOT-A-SECRET'}
        def cli(*args):
            return subprocess.run([sys.executable, str(root / 'demo.py'), '--offline', '--log', self.path, *args],
                                  capture_output=True, text=True, env=env, check=True).stdout
        output = cli()
        self.assertIn('MockPayPalClient', output)
        self.assertIn('created, awaiting buyer approval', output)
        oid = next(r['order_id'] for r in self.log.entries() if r.get('order_id'))
        output = cli('--capture', oid, '--mock-approve')
        self.assertIn('Paid: capture COMPLETED.', output)
        self.assertIn('Capture IDs: MOCK-CAP-', output)
        self.assertNotIn('FAKE-NOT-A-SECRET', output)
        self.assertTrue(self.log.verify())

    def test_sandbox_get_order_uses_get(self):
        c = SandboxPayPalClient('id', 'secret')
        with patch.object(c, '_auth', return_value={}), patch.object(c, '_req', return_value={}) as req:
            c.get_order('TEST123')
        self.assertEqual(req.call_args.args, ('GET', '/v2/checkout/orders/TEST123'))
