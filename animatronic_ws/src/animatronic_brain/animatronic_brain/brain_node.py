"""The brain ROS 2 node: /stt/text -> keyword/Ollama/fallback decision
-> /brain/response, with /brain/state and Ollama diagnostics.

Design note: /stt/text and /brain/state are plain std_msgs/String rather
than new custom messages -- animatronic_interfaces intentionally only
defines Response, AudioReady, and EspStatus. /stt/text's String payload
is a small JSON object {"request_id", "text", "session_id"} so the
request_id minted by web_bridge survives the hop to /brain/response.
"""

from __future__ import annotations

import json
import threading

import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger

from animatronic_common.snacks import SnacksStore
from animatronic_interfaces.msg import Response

from animatronic_brain.decision_engine import DecisionEngine
from animatronic_brain.ollama_client import OllamaClient, build_system_prompt
from animatronic_brain.line_picker import LineHistory


class BrainNode(Node):
    def __init__(self, **node_kwargs):
        super().__init__('brain_node', **node_kwargs)

        self._declare_params()
        g = self.get_parameter

        self._snacks = SnacksStore(
            g('snacks_config_path').value,
            logger=self.get_logger(),
        )

        self._client = OllamaClient(
            base_url=g('ollama_base_url').value,
            model=g('ollama_model').value,
            timeout_s=g('ollama_timeout_s').value,
            num_predict=g('ollama_num_predict').value,
            num_ctx=g('ollama_num_ctx').value,
            keep_alive=g('ollama_keep_alive').value,
        )

        self._engine = DecisionEngine(
            self._client,
            keyword_threshold=g('keyword_threshold').value,
            keyword_margin=g('keyword_margin').value,
            llm_reply=g('llm_reply').value,
            line_history=LineHistory(),
            logger=self.get_logger(),
        )

        # Guards Ollama calls so two /stt/text messages (shouldn't happen
        # given web_bridge's 1-in-flight cap, but defensively) never hit
        # Ollama concurrently -- see CLAUDE.md "Serialize requests".
        self._ollama_lock = threading.Lock()
        self._last_health_ok: bool | None = None

        self._response_pub = self.create_publisher(Response, '/brain/response', 10)
        self._state_pub = self.create_publisher(String, '/brain/state', 10)
        self._diagnostics_pub = self.create_publisher(DiagnosticArray, '/diagnostics', 10)

        self.create_subscription(String, '/stt/text', self._on_text, 10)
        self.create_service(Trigger, '~/reload', self._on_reload)

        reload_period = g('reload_poll_interval_s').value
        self.create_timer(reload_period, self._poll_reload)

        health_period = g('health_check_interval_s').value
        self.create_timer(health_period, self._publish_diagnostics)

        self.get_logger().info('warming up Ollama...')
        snacks = self._snacks.current
        ok = self._client.warm_up(snacks.ids(), build_system_prompt(snacks.snacks))
        self.get_logger().info(f'Ollama warm-up {"succeeded" if ok else "failed (will retry per-request)"}')
        self._publish_diagnostics()

    def _declare_params(self):
        defaults = {
            'snacks_config_path': 'config/snacks.json',
            'reload_poll_interval_s': 2.0,
            'keyword_threshold': 1,
            'keyword_margin': 1,
            'ollama_base_url': 'http://localhost:11434',
            'ollama_model': 'qwen2.5:0.5b',
            'ollama_timeout_s': 45.0,
            'ollama_num_predict': 16,
            'ollama_num_ctx': 1024,
            'ollama_keep_alive': -1,
            'health_check_interval_s': 30.0,
            'llm_reply': False,
        }
        for name, default in defaults.items():
            self.declare_parameter(name, default)

    def _on_text(self, msg: String):
        try:
            payload = json.loads(msg.data)
            request_id = payload['request_id']
            text = payload['text']
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            self.get_logger().error(f"malformed /stt/text payload: {e}")
            return

        self._state_pub.publish(String(data='thinking'))
        try:
            with self._ollama_lock:
                decision = self._engine.decide(request_id, text, self._snacks.current)
        finally:
            self._state_pub.publish(String(data='idle'))

        response = Response(
            request_id=decision.request_id,
            text=decision.text,
            snack_id=decision.snack_id,
            line_id=decision.line_id,
            source=decision.source,
        )
        self._response_pub.publish(response)
        self.get_logger().info(
            f"[{decision.request_id}] -> snack={decision.snack_id} source={decision.source}"
        )

    def _on_reload(self, request, response):
        ok, message = self._snacks.force_reload()
        response.success = ok
        response.message = message
        return response

    def _poll_reload(self):
        self._snacks.maybe_reload()

    def _publish_diagnostics(self):
        ok = self._client.health_check()
        self._last_health_ok = ok

        status = DiagnosticStatus()
        status.name = 'brain: ollama'
        status.hardware_id = 'ollama'
        if ok:
            status.level = DiagnosticStatus.OK
            status.message = 'Ollama reachable'
        else:
            status.level = DiagnosticStatus.ERROR
            status.message = 'Ollama unreachable (keyword scoring + fallback still available)'

        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()
        array.status = [status]
        self._diagnostics_pub.publish(array)


def main(args=None):
    rclpy.init(args=args)
    node = BrainNode()
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
