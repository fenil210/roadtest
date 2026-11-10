"""roadtest.yaml: targets, environment variables, unknown keys and model settings."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from roadtest.config import load_config
from roadtest.errors import ConfigError

CONFIG = """
    version: 1
    agent:
      url: http://localhost:8000/chat
      timeout: 30
      headers:
        X-Team: core
        X-Region: "${ROADTEST_TEST_REGION:-eu}"
        Authorization: "Bearer ${ROADTEST_TEST_TOKEN}"
      body:
        message: "{{message}}"
      response:
        text: reply
    targets:
      staging:
        url: https://staging.example.com/chat
      prod:
        url: https://api.example.com/chat
        headers:
          Authorization: "Bearer ${ROADTEST_TEST_PROD_TOKEN}"
"""


def write_config(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "roadtest.yaml"
    path.write_text(dedent(text), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ROADTEST_TEST_REGION", "ROADTEST_TEST_TOKEN", "ROADTEST_TEST_PROD_TOKEN", "ROADTEST_CONFIG"):
        monkeypatch.delenv(name, raising=False)


def test_a_named_target_inherits_the_agent_and_overrides_only_its_own_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A target that lost the agent's body or response paths would send the wrong request to staging.
    monkeypatch.setenv("ROADTEST_TEST_TOKEN", "local-token")
    monkeypatch.setenv("ROADTEST_TEST_PROD_TOKEN", "prod-token")
    config = load_config(write_config(tmp_path, CONFIG))

    default = config.agent_for()
    assert default.url == "http://localhost:8000/chat"
    assert default.headers == {"X-Team": "core", "X-Region": "eu", "Authorization": "Bearer local-token"}

    staging = config.agent_for("staging")
    assert staging.url == "https://staging.example.com/chat"
    assert staging.timeout == 30
    assert staging.response.text == "reply"
    assert staging.body == {"message": "{{message}}"}
    assert staging.headers == default.headers

    prod = config.agent_for("prod")
    assert prod.url == "https://api.example.com/chat"
    assert prod.headers == {"X-Team": "core", "X-Region": "eu", "Authorization": "Bearer prod-token"}

    # A mistyped target must stop the run, not quietly test the default agent.
    with pytest.raises(ConfigError, match="no target named `stagin`.*staging"):
        config.agent_for("stagin")


def test_environment_variables_are_read_when_a_target_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A missing production token must not stop a local run, and must be named when production is used.
    config = load_config(write_config(tmp_path, CONFIG))  # nothing is set yet, and loading still works

    with pytest.raises(ConfigError) as missing_local:
        config.agent_for()
    assert "ROADTEST_TEST_TOKEN" in str(missing_local.value)
    assert "agent.headers.Authorization" in str(missing_local.value)

    monkeypatch.setenv("ROADTEST_TEST_TOKEN", "local-token")
    assert config.agent_for("staging").headers["Authorization"] == "Bearer local-token"

    with pytest.raises(ConfigError) as missing_prod:
        config.agent_for("prod")
    assert "ROADTEST_TEST_PROD_TOKEN" in str(missing_prod.value)
    assert "targets.prod.headers.Authorization" in str(missing_prod.value)

    monkeypatch.setenv("ROADTEST_TEST_PROD_TOKEN", "prod-token")
    assert config.agent_for("prod").headers["Authorization"] == "Bearer prod-token"


@pytest.mark.parametrize(
    "yaml_text, expected",
    [
        pytest.param("agent: {url: 'http://x'}\nmodles: {judge: a}\n", "roadtest.yaml: modles: unknown key", id="top-level"),
        pytest.param("agent: {url: 'http://x', timout: 5}\n", "roadtest.yaml: agent: timout: unknown key", id="agent"),
        pytest.param("agent: {url: 'http://x', response: {txt: reply}}\n", "agent: response.txt: unknown key", id="response"),
    ],
)
def test_a_mistyped_key_is_rejected_with_where_it_is(yaml_text: str, expected: str, tmp_path: Path) -> None:
    # Silently ignoring a typo like `timout` leaves the default in force and the user none the wiser.
    config_path = write_config(tmp_path, yaml_text)
    with pytest.raises(ConfigError) as error:
        load_config(config_path).agent_for()
    assert expected in str(error.value)


def test_a_model_is_a_plain_id_or_a_mapping(tmp_path: Path) -> None:
    config = load_config(
        write_config(
            tmp_path,
            """
                agent: {url: "http://x"}
                models:
                  judge: my-judge
                  generator:
                    model: my-generator
                    reasoning_effort: low
                    params: {temperature: 0}
            """,
        )
    )
    assert config.models.judge.model == "my-judge"
    assert config.models.judge.reasoning_effort is None
    assert config.models.generator.model == "my-generator"
    assert config.models.generator.reasoning_effort == "low"
    assert config.models.generator.params == {"temperature": 0}
