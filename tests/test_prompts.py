"""What the judge and the simulated user are told. These are plain functions; no model is called."""

from __future__ import annotations

from roadtest import ToolCall
from roadtest.models import judge, simulated_user
from roadtest.scenarios import Simulate

CONVERSATION = [
    {"role": "user", "content": "Please refund order A-1002."},
    {"role": "assistant", "content": "I looked it up. What is the reason for the refund?"},
    {"role": "user", "content": "It arrived broken."},
]


def test_the_judge_is_given_the_criterion_the_reply_and_the_conversation_it_belongs_to() -> None:
    # A judge that never sees the reply (or the question) grades nothing, but still returns a verdict.
    messages = judge.build_messages(
        "Declines the refund and explains the 30-day window.",
        CONVERSATION,
        "Sorry, that order is outside the 30-day window.",
        [ToolCall("lookup_order", {"order_id": "A-1002"})],
    )
    assert [message["role"] for message in messages] == ["system", "user"]
    prompt = messages[1]["content"]
    assert "<criterion>\nDeclines the refund and explains the 30-day window.\n</criterion>" in prompt
    assert "<reply_being_graded>\nSorry, that order is outside the 30-day window.\n</reply_being_graded>" in prompt
    assert "[user]\nPlease refund order A-1002." in prompt
    assert "[agent]\nI looked it up. What is the reason for the refund?" in prompt
    assert 'lookup_order({"order_id": "A-1002"})' in prompt


def test_a_whole_conversation_is_graded_without_a_reply_section() -> None:
    # Conversation-level rubric lines have no single reply. Sending a placeholder reply would be graded as one.
    prompt = judge.build_messages("Asks for the reason before refunding.", CONVERSATION, None, [])[1]["content"]
    assert "<reply_being_graded>" not in prompt
    assert "[agent]\nI looked it up. What is the reason for the refund?" in prompt
    assert "<criterion>\nAsks for the reason before refunding.\n</criterion>" in prompt


def test_the_judge_can_tell_no_tools_called_from_tool_calls_not_reported() -> None:
    # "The agent called no tools" lets a judge fail "says it checked the order" correctly. "We cannot see the
    # tools" must not read the same way, or every reply that mentions a lookup is failed as invented.
    none_called = judge.build_messages("Says it looked the order up.", CONVERSATION, "I checked.", [])[1]["content"]
    not_reported = judge.build_messages("Says it looked the order up.", CONVERSATION, "I checked.", None)[1]["content"]
    assert "<tools_called>\n(none)\n</tools_called>" in none_called
    assert "does not report its tool calls" not in none_called
    assert "<tools_called>\n(this agent does not report its tool calls)\n</tools_called>" in not_reported
    assert "(none)" not in not_reported


def test_the_simulated_user_sees_the_conversation_from_its_own_side() -> None:
    # If the roles were swapped the generator would answer as the agent and the conversation would never move.
    messages = simulated_user.build_messages(
        Simulate(goal="Get a refund for the broken helmet.", persona="An impatient customer."), CONVERSATION
    )
    prompt = messages[1]["content"]
    assert "<persona>\nAn impatient customer.\n</persona>" in prompt
    assert "<goal>\nGet a refund for the broken helmet.\n</goal>" in prompt
    assert "[you]\nPlease refund order A-1002." in prompt
    assert "[agent]\nI looked it up. What is the reason for the refund?" in prompt
    assert prompt.rindex("[you]\nIt arrived broken.") > prompt.rindex("[agent]")


def test_the_simulated_user_is_told_to_speak_first_when_nothing_has_been_said() -> None:
    prompt = simulated_user.build_messages(Simulate(goal="Ask where order A-1003 is."), [])[1]["content"]
    assert "you speak first" in prompt
    assert "<persona>\nAn ordinary user.\n</persona>" in prompt
