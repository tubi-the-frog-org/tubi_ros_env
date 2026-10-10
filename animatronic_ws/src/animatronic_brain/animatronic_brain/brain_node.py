"""The brain ROS 2 node: /stt/text -> LlmProvider chain decision ->
/brain/response, with /brain/state and provider diagnostics.

Design note: /stt/text and /brain/state are plain std_msgs/String rather
than new custom messages -- animatronic_interfaces intentionally only
defines Response, AudioReady, and EspStatus. /stt/text's String payload
is a small JSON object {"request_id", "text", "session_id"} so the
request_id minted by web_bridge survives the hop to /brain/response.
"""

from __future__ import annotations

import json
import os
import threading

import rclpy
from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus
from rclpy.node import Node
from std_msgs.msg import String
from std_srvs.srv import Trigger

from animatronic_common.snacks import SnacksStore
from animatronic_interfaces.msg import Response

from animatronic_brain.cloud_client import CloudLlmClient
from animatronic_brain.decision_engine import DecisionEngine
from animatronic_brain.line_picker import LineHistory
from animatronic_brain.llm_provider import (
    CloudProvider,
    FallbackProvider,
    KeywordProvider,
    LocalProvider,
    build_provider_chain,
)
from animatronic_brain.ollama_client import OllamaClient, build_system_prompt
from animatronic_brain.request_budget import RequestBudget


class BrainNode(Node):
    def __init__(self, **node_kwargs):
        super().__init__('brain_node', **node_kwargs)

        self._declare_params()
        g = self.get_parameter

        self._snacks = SnacksStore(
            g('snacks_config_path').value,
            logger=self.get_logger(),
        )

        self._local_enabled = g('local_enabled').value
        self._cloud_enabled = g('cloud_enabled').value

        ollama_client = OllamaClient(
            base_url=g('ollama_base_url').value,
            model=g('ollama_model').value,
            timeout_s=g('ollama_timeout_s').value,
            num_predict=g('ollama_num_predict').value,
            num_ctx=g('ollama_num_ctx').value,
            keep_alive=g('ollama_keep_alive').value,
        )

        api_key = os.environ.get('LLM_API_KEY', '')
        if self._cloud_enabled and not api_key:
            self.get_logger().warning('cloud provider enabled but LLM_API_KEY is not set in the environment')
        cloud_client = CloudLlmClient(
            base_url=g('cloud_base_url').value,
            model=g('cloud_model').value,
            api_key=api_key,
            timeout_s=g('cloud_timeout_s').value,
            temperature=g('cloud_temperature').value,
        )

        self._budget = RequestBudget(
            daily_limit=g('cloud_daily_request_cap').value,
            total_limit=g('cloud_total_request_cap').value,
        )

        providers_by_name = {
            'keyword': KeywordProvider(
                threshold=g('keyword_threshold').value,
                margin=g('keyword_margin').value,
            ),
            'cloud': CloudProvider(
                cloud_client,
                self._budget,
                enabled=self._cloud_enabled,
                logger=self.get_logger(),
            ),
            'local': LocalProvider(
                ollama_client,
                enabled=self._local_enabled,
                logger=self.get_logger(),
            ),
            'fallback': FallbackProvider(),
        }
        chain_names = list(g('provider_chain').value)
        chain = build_provider_chain(chain_names, providers_by_name)
        if not chain or chain[-1].name != 'fallback':
            self.get_logger().warning(
                f"provider_chain {chain_names} doesn't end in 'fallback' -- "
                "decision_engine will still hard-fallback if the chain is exhausted"
            )

        self._ollama_client = ollama_client
        self._engine = DecisionEngine(chain, line_history=LineHistory(), logger=self.get_logger())

        # Guards LLM calls so two /stt/text messages (shouldn't happen given
        # web_bridge's 1-in-flight cap, but defensively) never hit a provider
        # concurrently -- see CLAUDE.md "Serialize requests".
        self._provider_lock = threading.Lock()

        self._response_pub = self.create_publisher(Response, '/brain/response', 10)
        self._state_pub = self.create_publisher(String, '/brain/state', 10)
        self._diagnostics_pub = self.create_publisher(DiagnosticArray, '/diagnostics', 10)

        self.create_subscription(String, '/stt/text', self._on_text, 10)
        self.create_service(Trigger, '~/reload', self._on_reload)

        reload_period = g('reload_poll_interval_s').value
        self.create_timer(reload_period, self._poll_reload)

        if self._local_enabled:
            self.get_logger().info('warming up Ollama (local provider enabled)...')
            snacks = self._snacks.current
            ok = ollama_client.warm_up(snacks.ids(), build_system_prompt(snacks.snacks))
            self.get_logger().info(f'Ollama warm-up {"succeeded" if ok else "failed (will retry per-request)"}')

        health_period = g('health_check_interval_s').value
        self.create_timer(health_period, self._publish_diagnostics)
        self._publish_diagnostics()

    def _declare_params(self):
        defaults = {
            'snacks_config_path': 'config/snacks.json',
            'reload_poll_interval_s': 2.0,

            'provider_chain': ['keyword', 'cloud', 'fallback'],

            'keyword_threshold': 1,
            'keyword_margin': 1,

            'cloud_enabled': True,
            'cloud_base_url': 'https://api.openai.com/v1',
            'cloud_model': 'gpt-5-nano',
            'cloud_timeout_s': 6.0,
            'cloud_temperature': 0.7,
            'cloud_daily_request_cap': 300,
            'cloud_total_request_cap': -1,

            'local_enabled': False,
            'ollama_base_url': 'http://localhost:11434',
            'ollama_model': 'qwen2.5:0.5b',
            'ollama_timeout_s': 45.0,
            'ollama_num_predict': 16,
            'ollama_num_ctx': 1024,
            'ollama_keep_alive': -1,
            'health_check_interval_s': 30.0,
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
            with self._provider_lock:
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
        statuses = [self._cloud_budget_status()]
        if self._local_enabled:
            statuses.append(self._local_health_status())

        array = DiagnosticArray()
        array.header.stamp = self.get_clock().now().to_msg()
        array.status = statuses
        self._diagnostics_pub.publish(array)

    def _cloud_budget_status(self) -> DiagnosticStatus:
        """No network call -- just reports the in-memory counters, so this
        is safe to publish on every timer tick regardless of cloud_enabled."""
        status = DiagnosticStatus()
        status.name = 'brain: cloud request budget'
        status.hardware_id = 'cloud_llm'
        if not self._cloud_enabled:
            status.level = DiagnosticStatus.OK
            status.message = 'cloud provider disabled (kill switch)'
        elif self._budget.daily_limit != -1 and self._budget.daily_count >= self._budget.daily_limit:
            status.level = DiagnosticStatus.WARN
            status.message = f'daily cap reached: {self._budget.daily_count}/{self._budget.daily_limit}'
        else:
            status.level = DiagnosticStatus.OK
            status.message = f'daily={self._budget.daily_count}/{self._budget.daily_limit} total={self._budget.total_count}/{self._budget.total_limit}'
        return status

    def _local_health_status(self) -> DiagnosticStatus:
        ok = self._ollama_client.health_check()
        status = DiagnosticStatus()
        status.name = 'brain: local (ollama)'
        status.hardware_id = 'ollama'
        status.level = DiagnosticStatus.OK if ok else DiagnosticStatus.ERROR
        status.message = 'Ollama reachable' if ok else 'Ollama unreachable'
        return status


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
