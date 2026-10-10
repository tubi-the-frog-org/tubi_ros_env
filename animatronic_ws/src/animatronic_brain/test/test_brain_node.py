"""Smoke test for the rclpy wiring: node constructs and publishing a
/stt/text-shaped message produces a Response on /brain/response, both
via the keyword path (no network) and via the cloud path (fake
OpenAI-compatible server). Deeper pipeline behavior (provider
precedence, fallback, validation) is covered by test_decision_engine.py
and test_llm_provider.py without rclpy.
"""

import json

import pytest
import rclpy
from rclpy.node import Node
from std_msgs.msg import String

from animatronic_brain.brain_node import BrainNode
from animatronic_interfaces.msg import Response
from fake_cloud_server import FakeCloudServer


@pytest.fixture
def rclpy_context():
    rclpy.init()
    yield
    rclpy.shutdown()


def _write_snacks(tmp_path):
    path = tmp_path / "snacks.json"
    path.write_text(json.dumps({
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
    return path


def _make_node(snacks_path, extra_params):
    params = [
        '-p', f'snacks_config_path:={snacks_path}',
        '-p', 'reload_poll_interval_s:=9999.0',
        '-p', 'health_check_interval_s:=9999.0',
        *extra_params,
    ]
    return BrainNode(cli_args=['--ros-args', *params])


def _publish_and_collect_response(node, request_id, text):
    listener = Node('test_listener')
    received = []
    listener.create_subscription(Response, '/brain/response', lambda m: received.append(m), 10)
    try:
        node._on_text(String(data=json.dumps({"request_id": request_id, "text": text})))
        for _ in range(20):
            rclpy.spin_once(node, timeout_sec=0.05)
            rclpy.spin_once(listener, timeout_sec=0.05)
            if received:
                break
        return received
    finally:
        listener.destroy_node()


def test_brain_node_publishes_response_for_keyword_match(rclpy_context, tmp_path):
    snacks_path = _write_snacks(tmp_path)
    node = _make_node(snacks_path, [])
    try:
        received = _publish_and_collect_response(node, "req-abc", "something crunchy and salty")
        assert len(received) == 1
        assert received[0].request_id == "req-abc"
        assert received[0].snack_id == "chips"
        assert received[0].source == "keyword"
    finally:
        node.destroy_node()


def test_brain_node_publishes_response_for_cloud_path(rclpy_context, tmp_path, monkeypatch):
    monkeypatch.setenv('LLM_API_KEY', 'sk-test')
    snacks_path = _write_snacks(tmp_path)

    with FakeCloudServer(responses=[{"status": 200, "snack": "none", "reply": "No snack calls to you, traveler."}]) as server:
        node = _make_node(snacks_path, [
            '-p', 'provider_chain:=[cloud,fallback]',  # skip keyword so this exercises the cloud path
            '-p', f'cloud_base_url:={server.url}',
        ])
        try:
            received = _publish_and_collect_response(node, "req-xyz", "totally unrelated text")
            assert len(received) == 1
            assert received[0].request_id == "req-xyz"
            assert received[0].snack_id == "none"
            assert received[0].source == "cloud"
            assert received[0].text == "No snack calls to you, traveler."
        finally:
            node.destroy_node()


def test_brain_node_malformed_payload_does_not_crash(rclpy_context, tmp_path):
    snacks_path = _write_snacks(tmp_path)
    node = _make_node(snacks_path, [])
    try:
        node._on_text(String(data="not json"))  # should log and return, not raise
    finally:
        node.destroy_node()
