"""SIMULATED tools. Nothing here sends, books, pays or contacts anyone. Ever.

Each tool has a plain description (the router reads it) and an optional floor. A floor of
"review" means code, not a model, has decided this tool always gets a second opinion.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    floor: str | None = None      # None or "review"


TOOLS: dict[str, Tool] = {t.name: t for t in (
    Tool("notes.append", "Append a line to the user's own private notes file. Nobody else can see it. "
                         "It can be edited or deleted at any time."),
    Tool("calendar.hold", "Put a private hold on the user's own calendar. No guests, no notifications. "
                          "It can be removed at any time."),
    Tool("email.create_draft", "Save an email as a draft in the user's own mailbox. Nothing is sent. "
                               "The draft can be edited or deleted."),
    Tool("events.search", "Look up public event listings. Read only. Changes nothing."),
    Tool("label.add", "Add a private label to a message in the user's own mailbox. It can be removed."),
    Tool("email.send", "Send an email from the user's address to the recipient. Once sent it cannot be recalled."),
    Tool("event.rsvp", "Register the user for an event under their name. The host sees the registration "
                       "and may hold a seat for them."),
    Tool("status.report", "Tell the user that something is true, as a settled fact they will plan around."),
    Tool("calendar.invite", "Send a calendar invitation to other people. Each guest gets an email at once."),
    Tool("booking.reserve", "Book a seat, table or ticket in the user's name. The venue is notified and "
                            "cancellation may carry a fee."),
    Tool("payment.pay", "Charge the user's card for the stated amount. A charge cannot be undone from here.",
         floor="review"),
)}


def describe(name: str) -> str:
    tool = TOOLS.get(name)
    return tool.description if tool else "Unknown tool. No description is registered."


def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Pretend to run the tool. Returns what would have happened."""
    target = args.get("to") or args.get("event") or args.get("title") or args.get("payee") or ""
    return {
        "simulated": True,
        "tool": name,
        "result": f"SIMULATED: {name} would have run{(' for ' + str(target)) if target else ''}. Nothing was sent.",
    }
