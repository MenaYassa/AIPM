"""MC-6.16-D2.2 Gate B B1.1: IPC Transport Integrity Test.

Proves that action_protocol from trusted UpdateExecutionBinding crosses
the IPC boundary intact to ExecutionRequest for both mc616d2-v1 and legacy-v1.
"""
import pytest
from aipm.control_plane.models import UpdateExecutionBinding
from aipm.control_plane.executor_ipc import ExecutionRequest
from aipm.composition.executor_update import compose_ipc_update_runtime


class FakeIPCClient:
    """Captures the serialized ExecutionRequest sent over IPC."""
    def __init__(self):
        self.sent_request = None

    def send(self, request: ExecutionRequest):
        self.sent_request = request
        from aipm.control_plane.executor_ipc import ExecutionResponse
        return ExecutionResponse(
            outcome="succeeded",
            provider_code="test_ok",
            action_id=request.action_id,
            evidence_reference="",
        )


@pytest.mark.parametrize("protocol", ["mc616d2-v1", "legacy-v1"])
def test_binding_action_protocol_crosses_ipc_intact(protocol):
    """Prove: trusted binding action_protocol == IPC payload action_protocol."""
    binding = UpdateExecutionBinding(
        project_name="searxng-stack",
        plan_digest="a" * 64,
        confirmation_id="b" * 32,
        action_id="c" * 64,
        contract_digest="d" * 64,
        lease_id="e" * 32,
        fencing_token=1,
        action_protocol=protocol,
    )

    client = FakeIPCClient()
    runtime = compose_ipc_update_runtime(client)

    # Execute runtime with trusted binding
    runtime(binding)

    # 1. Verify ExecutionRequest captured contains identical protocol
    assert client.sent_request is not None
    assert client.sent_request.action_protocol == binding.action_protocol
    assert client.sent_request.action_protocol == protocol

    # 2. Verify wire round-trip (serialization -> deserialization)
    wire_bytes = client.sent_request.to_json()
    deserialized = ExecutionRequest.from_json(wire_bytes)

    assert deserialized.action_protocol == binding.action_protocol
    assert deserialized.action_protocol == protocol
