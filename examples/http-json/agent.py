"""The front desk of a made-up library, as a JSON HTTP agent.

The client chooses the conversation id, and the agent remembers the library card number for
that conversation. It is rule-based: no model, no API key, and the same answer every time.

    python3 agent.py --port 18801

    POST /chat  {"question": "Do you have Kindred?", "conversation_id": "any-string"}
    200         {"data": {"answer": "...", "steps": [{"tool": "search_catalogue", "input": {...}}]}}
"""

from __future__ import annotations

import argparse
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

DEFAULT_PORT = 18801

MEMBERS = {
    "M-2041": {
        "The Left Hand of Darkness": {"due": "14 March", "renewed_to": "4 April", "reserved": False},
        "Dune": {"due": "20 March", "renewed_to": "10 April", "reserved": True},
    },
}
CATALOGUE = {
    "Kindred": {"author": "Octavia E. Butler", "shelf": "F-BUT", "on_shelf": True},
    "Dune": {"author": "Frank Herbert", "shelf": "F-HER", "on_shelf": False},
    "The Left Hand of Darkness": {"author": "Ursula K. Le Guin", "shelf": "F-LEG", "on_shelf": False},
}
HOURS = "We are open Monday to Saturday, 9:00 to 18:00, and closed on Sundays."
SCOPE = "I can help with opening hours, the catalogue, and your loans and renewals at Maple Street Library."
ASK_FOR_CARD = "What is your library card number? It looks like M-1234."

_CARD = re.compile(r"\bM-\d{4}\b", re.IGNORECASE)
_conversations: dict[str, dict[str, Any]] = {}
_lock = threading.Lock()


def respond(question: str, conversation_id: str) -> dict[str, Any]:
    """One turn: the answer and the tools that were used to write it."""
    with _lock:
        memory = _conversations.setdefault(conversation_id, {})
    text = question.lower()
    steps: list[dict[str, Any]] = []

    def use(tool: str, **tool_input: Any) -> None:
        steps.append({"tool": tool, "input": tool_input})

    found = _CARD.search(question)
    if found:
        memory["card"] = found.group(0).upper()
    card = memory.get("card")
    title = next((name for name in CATALOGUE if name.lower() in text), None)

    if "renew" in text:
        if not card:
            answer = f"I can renew that for you. {ASK_FOR_CARD}"
        else:
            use("list_loans", card=card)
            loans = MEMBERS.get(card)
            if loans is None:
                answer = f"I could not find a member with card {card}. Could you check the number?"
            elif title not in loans:
                answer = f"I can't see that book on card {card}. You have on loan: {_listed(loans)}."
            elif loans[title]["reserved"]:
                answer = (
                    f"{title} can't be renewed because another member has reserved it. "
                    f"It is due back on {loans[title]['due']}."
                )
            else:
                use("renew_loan", card=card, title=title)
                answer = f"Done. {title} is renewed and is now due back on {loans[title]['renewed_to']}."
    elif any(phrase in text for phrase in ("on loan", "my loans", "borrowed", "have out", "due back")):
        if not card:
            answer = f"I can check that for you. {ASK_FOR_CARD}"
        else:
            use("list_loans", card=card)
            loans = MEMBERS.get(card)
            if loans is None:
                answer = f"I could not find a member with card {card}. Could you check the number?"
            else:
                answer = f"You have {len(loans)} books on loan: {_listed(loans)}."
    elif any(word in text for word in ("open", "close", "hours")):
        answer = HOURS
    elif title or any(phrase in text for phrase in ("do you have", "looking for", "copy of", "catalogue")):
        use("search_catalogue", title=title or question.strip())
        book = CATALOGUE.get(title or "")
        if book is None:
            answer = "I could not find that title in our catalogue. Could you check the spelling?"
        elif book["on_shelf"]:
            answer = f"Yes. {title} by {book['author']} is on the shelf at {book['shelf']}."
        else:
            answer = f"We have {title} by {book['author']}, but every copy is out on loan at the moment."
    elif found:
        answer = f"Thanks, I have your card {card}. What would you like to do?"
    elif any(word in text.split() for word in ("hello", "hi", "hey")):
        answer = f"Hello! {SCOPE}"
    else:
        answer = f"That is outside what I can do. {SCOPE}"
    return {"data": {"answer": answer, "steps": steps}}


def _listed(loans: dict[str, dict[str, Any]]) -> str:
    return " and ".join(f"{title} (due {loan['due']})" for title, loan in loans.items())


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - the name is set by BaseHTTPRequestHandler
        if self.path.rstrip("/") != "/chat":
            self._send(404, {"error": "POST /chat"})
            return
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
            question, conversation_id = body["question"], body["conversation_id"]
            if not isinstance(question, str) or not isinstance(conversation_id, str) or not conversation_id:
                raise TypeError
        except (ValueError, KeyError, TypeError):
            self._send(400, {"error": 'send JSON like {"question": "...", "conversation_id": "..."}'})
            return
        self._send(200, respond(question, conversation_id))

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
    """Start the agent on a background thread. Port 0 picks a free port. Returns (server, chat URL)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/chat"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the library desk agent.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    arguments = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", arguments.port), _Handler)
    print(f"Library desk agent on http://127.0.0.1:{arguments.port}/chat  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
