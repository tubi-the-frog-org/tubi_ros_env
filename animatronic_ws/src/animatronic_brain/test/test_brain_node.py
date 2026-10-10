"""Smoke test for the rclpy wiring: node constructs, warms up against a
fake Ollama server, and publishing a /stt/text-shaped message produces
a Response on /brain/response. Deeper pipeline behavior (keyword vs
LLM vs fallback) is covered by test_decision_engine.py without rclpy.
"""

import json

import pytest
import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from animatronic_brain.brain_node import BrainNode
from animatronic_interfaces.msg import Response
from fake_ollama_server import FakeOllamaServer


@pytest.fixture
def rclpy_context():
    rclpy.init()
    yield
    rclpy.shutdown()


def _make_node(rclpy_context, snacks_path, ollama_url):
    return BrainNode(
        parameter_overrides=[],
        cli_args=[
            '--ros-args',
            '-p', f'snacks_config_path:={snacks_path}',
            '-p', f'ollama_base_url:={ollama_url}',
            '-p', 'reload_poll_interval_s:=9999.0',
            '-p', 'health_check_interval_s:=9999.0',
        ],
    )


def test_brain_node_publishes_response_for_keyword_match(rclpy_context, tmp_path):
    snacks_path = tmp_path / "snacks.json"
    snacks_path.write_text(json.dumps({
        "version": 1,
        "default_snack": "none",
        "snacks": [
            {"id": "chips", "label": "Chips", "slot": 3, "angle": 195,
             "aliases": [], "tags": ["salty", "crunchy"],
             "lines": [{"id": "chips_1", "text": "chips!"}]},
            {"id": "none", "label": "No snack", "slot": 0, "angle": 0,
             "aliases": [], "tags": [],
             "lines": [{"id": "none_1", "text": "nothing."}]},
        ],
    }))

    with FakeOllamaServer(chat_choice="none") as server:
        node = _make_node(rclpy_context, str(snacks_path), server.url)
        listener = Node('test_listener')
        received = []
        listener.create_subscription(Response, '/brain/response', lambda m: received.append(m), 10)

        try:
            node._on_text(String(data=json.dumps({
                "request_id": "req-abc",
                "text": "something crunchy and salty",
            })))

            for _ in range(20):
                rclpy.spin_once(node, timeout_sec=0.05)
                rclpy.spin_once(listener, timeout_sec=0.05)
                if received:
                    break

            assert len(received) == 1
            assert received[0].request_id == "req-abc"
            assert received[0].snack_id == "chips"
            assert received[0].source == "keyword"
        finally:
            node.destroy_node()
            listener.destroy_node()


def test_brain_node_malformed_payload_does_not_crash(rclpy_context, tmp_path):
    snacks_path = tmp_path / "snacks.json"
    snacks_path.write_text(json.dumps({
        "version": 1,
        "default_snack": "none",
        "snacks": [
            {"id": "none", "label": "No snack", "slot": 0, "angle": 0,
             "aliases": [], "tags": [],
             "lines": [{"id": "none_1", "text": "nothing."}]},
        ],
    }))

    with FakeOllamaServer(chat_choice="none") as server:
        node = _make_node(rclpy_context, str(snacks_path), server.url)
        try:
            node._on_text(String(data="not json"))  # should log and return, not raise
        finally:
            node.destroy_node()
