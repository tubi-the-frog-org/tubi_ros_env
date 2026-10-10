import pytest

from animatronic_common.snacks import Line, Snack
from animatronic_brain.ollama_client import (
    OllamaClient,
    OllamaInvalidResponseError,
    OllamaRequestError,
    OllamaTimeoutError,
    build_schema,
    build_system_prompt,
)
from fake_ollama_server import FakeOllamaServer

SNACK_IDS = ["cookies", "candy", "chips", "none"]
COOKIES = Snack(id="cookies", label="Cookies", slot=1, angle=65,
                 aliases=[], tags=[], lines=[Line(id="cookies_1", text="cookies!")])


def test_build_schema_shape():
    schema = build_schema(SNACK_IDS)
    assert schema["type"] == "object"
    assert schema["properties"]["choice"]["enum"] == SNACK_IDS
    assert schema["required"] == ["choice"]


def test_build_system_prompt_mentions_each_snack():
    prompt = build_system_prompt([COOKIES])
    assert "cookies" in prompt
    assert "Cookies" in prompt


def test_choose_snack_success():
    with FakeOllamaServer(chat_choice="chips") as server:
        client = OllamaClient(base_url=server.url, model="qwen2.5:0.5b", timeout_s=5)
        choice = client.choose_snack("something crunchy", SNACK_IDS, "pick a snack")
        assert choice == "chips"


def test_choose_snack_bad_json_content_raises():
    with FakeOllamaServer(chat_content="not json at all") as server:
        client = OllamaClient(base_url=server.url, model="qwen2.5:0.5b", timeout_s=5)
        with pytest.raises(OllamaInvalidResponseError):
            client.choose_snack("hello", SNACK_IDS, "pick a snack")


def test_choose_snack_choice_outside_enum_raises():
    with FakeOllamaServer(chat_choice="not_a_real_snack") as server:
        client = OllamaClient(base_url=server.url, model="qwen2.5:0.5b", timeout_s=5)
        with pytest.raises(OllamaInvalidResponseError):
            client.choose_snack("hello", SNACK_IDS, "pick a snack")


def test_choose_snack_timeout_raises():
    with FakeOllamaServer(chat_choice="chips", delay_s=1.0) as server:
        client = OllamaClient(base_url=server.url, model="qwen2.5:0.5b", timeout_s=0.2)
        with pytest.raises(OllamaTimeoutError):
            client.choose_snack("hello", SNACK_IDS, "pick a snack")


def test_choose_snack_non_200_raises():
    with FakeOllamaServer(chat_choice="chips", status_code=500) as server:
        client = OllamaClient(base_url=server.url, model="qwen2.5:0.5b", timeout_s=5)
        with pytest.raises(OllamaRequestError):
            client.choose_snack("hello", SNACK_IDS, "pick a snack")


def test_choose_snack_connection_refused_raises():
    client = OllamaClient(base_url="http://127.0.0.1:1", model="qwen2.5:0.5b", timeout_s=2)
    with pytest.raises(OllamaRequestError):
        client.choose_snack("hello", SNACK_IDS, "pick a snack")


def test_health_check_true_on_200():
    with FakeOllamaServer(health_status_code=200) as server:
        client = OllamaClient(base_url=server.url, model="qwen2.5:0.5b")
        assert client.health_check() is True


def test_health_check_false_on_error_status():
    with FakeOllamaServer(health_status_code=500) as server:
        client = OllamaClient(base_url=server.url, model="qwen2.5:0.5b")
        assert client.health_check() is False


def test_health_check_false_on_connection_refused():
    client = OllamaClient(base_url="http://127.0.0.1:1", model="qwen2.5:0.5b")
    assert client.health_check() is False


def test_warm_up_true_on_success():
    with FakeOllamaServer(chat_choice="none") as server:
        client = OllamaClient(base_url=server.url, model="qwen2.5:0.5b", timeout_s=5)
        assert client.warm_up(SNACK_IDS, "pick a snack") is True


def test_warm_up_false_on_failure_does_not_raise():
    client = OllamaClient(base_url="http://127.0.0.1:1", model="qwen2.5:0.5b", timeout_s=2)
    assert client.warm_up(SNACK_IDS, "pick a snack") is False


def test_generate_prose_reply_returns_stripped_content():
    with FakeOllamaServer(chat_content="  A dramatic wizardly line!  ") as server:
        client = OllamaClient(base_url=server.url, model="qwen2.5:0.5b", timeout_s=5)
        text = client.generate_prose_reply("hello", COOKIES)
        assert text == "A dramatic wizardly line!"
