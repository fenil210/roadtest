"""Each way of connecting an agent, through the example projects in `examples/`.

Every example ships its own `roadtest.yaml`, scenarios and rule-based agent. These tests run that
real config against that real agent (or, where a test needs to see the requests, against a scripted
stand-in for the agent under test). The expected replies are written out by hand.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from roadtest.config import load_config

from conftest import EXAMPLES, example_config, json_reply, run_config, scripted_server, statuses, turn_of, write_project

ALL_EXAMPLES = ["http-json", "http-streaming", "openai-compatible", "python-function", "command"]


@pytest.mark.parametrize("name", ALL_EXAMPLES)
def test_every_example_passes_against_its_own_agent(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    # The README sends people here first. An example that no longer works means an adapter or a
    # response-path change broke a documented way of connecting an agent.
    monkeypatch.chdir(EXAMPLES / name)
    with example_config(name) as config:
        found = statuses(run_config(config))
    assert len(found) >= 3
    assert set(found.values()) == {"pass"}, found


def test_streamed_pieces_are_joined_and_tool_events_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    # A stream read as one piece (or only the last) gives the judge and the contains checks half an answer.
    monkeypatch.chdir(EXAMPLES / "http-streaming")
    with example_config("http-streaming") as config:
        run = run_config(config, "route-remembered")
    first = turn_of(run, "route-remembered", 0)
    assert first["reply"] == "Got it: Leeds to Manchester. I can tell you when the next train leaves or whether it is on time."
    second = turn_of(run, "route-remembered", 1)
    assert second["reply"] == "The 09:40 from Leeds to Manchester is running 10 minutes late."
    assert second["tool_calls"] == [
        {"name": "check_disruptions", "arguments": {"origin": "leeds", "destination": "manchester"}}
    ]


def test_a_server_issued_conversation_id_is_left_out_first_and_sent_back_after() -> None:
    # Sending an empty or null id on turn 1 makes servers reject the call; forgetting the id on
    # turn 2 starts a new conversation every turn, so nothing is ever remembered.
    def stream(number: int, body: dict) -> tuple[int, str, str, float]:
        events = 'data: {"conversation_id": "abc-123"}\n\ndata: {"delta": "Hi"}\n\ndata: {"delta": " there"}\n\n'
        return 200, "text/event-stream", events, 0.0

    with scripted_server(stream) as (url, received):
        with example_config("http-streaming", url=url) as config:
            run = run_config(config, "route-remembered")
    assert received == [
        {"message": "I'm going from Leeds to Manchester today."},
        {"message": "Is it on time?", "conversation_id": "abc-123"},
    ]
    assert turn_of(run, "route-remembered", 1)["reply"] == "Hi there"


def test_a_stateless_endpoint_gets_the_whole_conversation_on_every_turn(tmp_path: Path) -> None:
    # An endpoint with no memory (the OpenAI-style `messages` list) knows nothing of turn 1 unless
    # turn 2 carries it, including the agent's own earlier answer.
    def answer(number: int, body: dict) -> tuple[int, str, str, float]:
        return json_reply({"reply": f"answer {number}"})

    with scripted_server(answer) as (url, received):
        config_path = write_project(
            tmp_path,
            """
                scenarios:
                  - id: two-turns
                    turns:
                      - user: My name is Sam.
                      - user: What is my name?
                        expect: {contains: answer 2}
            """,
            agent="url: " + url + '\nbody:\n  model: any-model\n  messages: "{{messages}}"\nresponse:\n  text: reply\n',
        )
        run = run_config(load_config(config_path))
    assert statuses(run) == {"two-turns": "pass"}
    assert received == [
        {"model": "any-model", "messages": [{"role": "user", "content": "My name is Sam."}]},
        {
            "model": "any-model",
            "messages": [
                {"role": "user", "content": "My name is Sam."},
                {"role": "assistant", "content": "answer 1"},
                {"role": "user", "content": "What is my name?"},
            ],
        },
    ]


def test_openai_style_tool_calls_have_their_json_string_arguments_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(EXAMPLES / "openai-compatible")
    with example_config("openai-compatible") as config:
        run = run_config(config, "booking-uses-earlier-turns")
    second = turn_of(run, "booking-uses-earlier-turns", 1)
    assert second["reply"] == "Booked: a table for 4 on Friday at 20:30. We will hold it for 15 minutes after that."
    assert second["tool_calls"] == [
        {"name": "check_availability", "arguments": {"day": "friday", "party_size": 4}},
        {"name": "book_table", "arguments": {"day": "friday", "party_size": 4, "time": "20:30"}},
    ]


def test_python_adapter_state_belongs_to_one_conversation(monkeypatch: pytest.MonkeyPatch) -> None:
    # One conversation tells the desk about Biscuit; the next conversation (run afterwards, one at a
    # time) must start with an empty desk. State that leaks between conversations books the wrong pet.
    monkeypatch.chdir(EXAMPLES / "python-function")
    with example_config("python-function") as config:
        run = run_config(config, "books-known-pet", "asks-about-the-pet", concurrency=1)
    assert statuses(run) == {"books-known-pet": "pass", "asks-about-the-pet": "pass"}
    booked = turn_of(run, "books-known-pet", 1)
    assert booked["reply"] == "Booked: Biscuit sees Dr Okafor tomorrow at 09:30."
    assert booked["tool_calls"] == [
        {"name": "find_slots", "arguments": {"species": "dog"}},
        {"name": "book_appointment", "arguments": {"pet": "Biscuit", "vet": "Dr Okafor", "time": "09:30"}},
    ]


def test_a_command_receives_the_earlier_turns_on_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    # The second message only says "reserve it": without `history` in the JSON on stdin the program cannot know the tool.
    monkeypatch.chdir(EXAMPLES / "command")
    with example_config("command") as config:
        run = run_config(config, "reserve-remembers-tool")
    second = turn_of(run, "reserve-remembers-tool", 1)
    assert second["reply"] == "Reserved: pressure washer for 2 days, $60 in total. Collect it from the counter at 8:00."
    assert second["tool_calls"] == [
        {"name": "check_stock", "arguments": {"tool": "pressure washer"}},
        {"name": "reserve_tool", "arguments": {"tool": "pressure washer", "days": 2}},
    ]
