"""The demo agent: a support bot for a made-up bike shop.

It is rule-based, so it needs no model, no API key and no network, and its replies are the same
every run. It has two deliberate bugs so that a demo run has something to catch:

1. It refunds orders that are outside the 30-day refund window.
2. It answers Spanish messages in English.

Run it on its own with `python -m roadtest.demo.agent --port 8000`, then POST to /chat:

    {"message": "Where is my order A-1003?", "session_id": "any-string"}
"""

from __future__ import annotations

import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

ORDERS = {
    "A-1001": {"item": "Trail helmet", "price": 89.00, "status": "delivered", "days_since_delivery": 10},
    "A-1002": {"item": "City bike lock", "price": 35.00, "status": "delivered", "days_since_delivery": 45},
    "A-1003": {"item": "Gravel tyres", "price": 64.00, "status": "in transit", "arrives_in_days": 2},
}
REFUND_WINDOW_DAYS = 30
SCOPE = "I can help with order status, refunds and shipping questions for Pedal & Co."

_ORDER_ID = re.compile(r"\bA-\d{4}\b", re.IGNORECASE)
_sessions: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def respond(message: str, session_id: str) -> dict[str, Any]:
    """One turn: the reply text and the tools that were called to write it."""
    with _lock:
        memory = _sessions.setdefault(session_id, {})
    text = message.lower()
    tools: list[dict[str, Any]] = []

    def call(name: str, **arguments: Any) -> None:
        tools.append({"name": name, "arguments": arguments})

    found = _ORDER_ID.search(message)
    if found:
        memory["order_id"] = found.group(0).upper()
    order_id = memory.get("order_id")

    if ("ignore" in text and "instruction" in text) or "system prompt" in text:
        reply = f"I can't share or change my instructions. {SCOPE}"
    elif any(word in text for word in ("refund", "money back", "return it", "return my")):
        if not order_id:
            reply = "I can help with a refund. What is your order number? It looks like A-1234."
        else:
            call("lookup_order", order_id=order_id)
            order = ORDERS.get(order_id)
            if order is None:
                reply = f"I could not find order {order_id}. Could you check the number?"
            elif order["status"] != "delivered":
                reply = f"Order {order_id} has not been delivered yet, so I can't refund it. You can refuse the parcel when it arrives."
            else:
                # Bug 1: there is no check against REFUND_WINDOW_DAYS here.
                call("start_refund", order_id=order_id)
                reply = (
                    f"I've started a refund of €{order['price']:.2f} for order {order_id} ({order['item']}). "
                    "The money will be back on your card in 5 to 7 business days."
                )
    elif any(word in text for word in ("where", "status", "track", "arrive", "dónde", "pedido")):
        if not order_id:
            reply = "I can check that for you. What is your order number? It looks like A-1234."
        else:
            call("lookup_order", order_id=order_id)
            order = ORDERS.get(order_id)
            if order is None:
                reply = f"I could not find order {order_id}. Could you check the number?"
            elif order["status"] == "in transit":
                # Bug 2: the reply is always in English, whatever language the user wrote in.
                reply = f"Order {order_id} ({order['item']}) is on its way and should arrive in {order['arrives_in_days']} days."
            else:
                reply = f"Order {order_id} ({order['item']}) was delivered {order['days_since_delivery']} days ago."
    elif any(word in text for word in ("ship", "deliver", "postage")):
        call("search_help", query="shipping")
        reply = "We ship within the EU and the UK. Standard delivery takes 3 to 5 business days and is free for orders over €50."
    elif order_id and found:
        reply = f"Thanks, I have order {order_id}. What would you like to do with it?"
    elif any(word in text for word in ("thank", "thanks", "that's all", "bye")):
        reply = "You're welcome! Enjoy the ride."
    elif any(word in text for word in ("hello", "hi ", "hey")) or text.strip() in ("hi", "hello"):
        reply = f"Hello! {SCOPE}"
    else:
        reply = f"That is outside what I can do. {SCOPE}"
    return {"reply": reply, "tool_calls": tools, "session_id": session_id}


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - the name is set by BaseHTTPRequestHandler
        if self.path.rstrip("/") != "/chat":
            self._send(404, {"error": "POST /chat"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            message = str(body["message"])
        except (ValueError, KeyError, TypeError):
            self._send(400, {"error": 'send JSON like {"message": "...", "session_id": "..."}'})
            return
        time.sleep(0.25 + (len(message) % 7) * 0.08)  # a real agent takes a moment; this keeps the demo honest
        self._send(200, respond(message, str(body.get("session_id") or "default")))

    def do_GET(self) -> None:  # noqa: N802
        self._send(200, {"agent": "Pedal & Co. support (roadtest demo)", "chat": "POST /chat"})

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *arguments: Any) -> None:
        return None


def start(port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Start the demo agent on a background thread. Port 0 picks a free port. Returns (server, chat URL)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/chat"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the roadtest demo agent.")
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), _Handler)
    print(f"Pedal & Co. support agent on http://127.0.0.1:{arguments.port}/chat  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
