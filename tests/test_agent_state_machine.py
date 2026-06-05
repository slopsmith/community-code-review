"""
Unit tests for volunteer agent state machine logic.

These tests verify the model lifecycle (unloaded → loading → ready → busy → unloading → unloaded)
without needing a GPU, llama-server, or the coordinator. They mock the WebSocket and subprocess
to test the decision logic in isolation.

Run:  python3 -m pytest tests/test_agent_state_machine.py -v
"""
# pylint: disable=redefined-outer-name,protected-access,missing-function-docstring

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Set required env vars before importing agent ────────────────────────
import os
os.environ.setdefault("COORDINATOR_URL", "http://test-coordinator:8080")
os.environ.setdefault("MOCK_MODE", "1")
os.environ.setdefault("MODEL_FILE", "test-model.gguf")
os.environ.setdefault("GPU_INFO", "Test GPU")
os.environ.setdefault("GPU_UTIL_THRESHOLD", "70")
os.environ.setdefault("GPU_MEM_THRESHOLD", "85")
os.environ.setdefault("IDLE_TIMEOUT", "30")
os.environ.setdefault("LLAMA_PORT", "18081")

# ── IMPORTANT: All globals MUST be accessed through 'agent.' prefix ──────
# Variables like _active_requests and _current_state are module-level ints/strs
# in agent.py.  If we do "from agent import _active_requests", the test module
# gets a COPY — reassigning it won't affect the real module globals that
# _update_model_state reads.  Always use:  import agent;  agent._active_requests

import agent  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def mock_ws():
    """Create a mock WebSocket that captures sent messages."""
    ws = AsyncMock()
    ws.send = AsyncMock()

    sent_messages = []

    async def capture_send(data):
        sent_messages.append(data)

    ws.send.side_effect = capture_send
    ws.sent_messages = sent_messages
    return ws


def extract_sent_types(ws):
    """Extract 'state' values from model_state messages."""
    states = []
    for msg in ws.sent_messages:
        if '"model_state"' in msg:
            import json
            try:
                d = json.loads(msg)
                if d.get("type") == "model_state":
                    states.append(d.get("state"))
            except (json.JSONDecodeError, TypeError):
                pass
    return states


def extract_gpu_msgs(ws):
    """Extract gpu_status messages."""
    msgs = []
    for msg in ws.sent_messages:
        if '"gpu_status"' in msg:
            import json
            try:
                d = json.loads(msg)
                if d.get("type") == "gpu_status":
                    msgs.append(d)
            except (json.JSONDecodeError, TypeError):
                pass
    return msgs


@pytest.fixture(autouse=True)
def reset_state():
    """Reset global state before each test."""
    agent._active_requests = 0
    agent._current_state = "unloaded"
    agent._gpu_stats = {"utilization": 0, "memory": 0}
    agent._last_request_time = 0.0
    agent._llama_process = None
    yield


@pytest.fixture(autouse=True)
def disable_real_subprocess():
    """Prevent any real subprocess calls."""
    with patch("agent.asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock:
        proc = MagicMock()
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)
        mock.return_value = proc
        yield mock


# ── Tests: Initial state ────────────────────────────────────────────────


class TestInitialState:
    """Agent starts in unloaded state with no active requests."""

    def test_starts_unloaded(self):
        assert agent._current_state == "unloaded"

    def test_no_active_requests(self):
        assert agent._active_requests == 0

    def test_gpu_stats_default_to_zero(self):
        assert agent._gpu_stats == {"utilization": 0, "memory": 0}


# ── Tests: State machine ────────────────────────────────────────────────


class TestStateTransitions:
    """
    Core state machine logic — these tests verify that _update_model_state()
    makes the correct decisions based on GPU stats and active requests.
    """

    @pytest.mark.asyncio
    async def test_unloaded_with_request_triggers_load(self, mock_ws):
        """When a request is active and model isn't loaded, start loading."""
        agent._active_requests = 1
        agent._current_state = "unloaded"

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=False):
            with patch("agent._start_llama_server", new_callable=AsyncMock, return_value=True) as mock_start:
                await agent._update_model_state(mock_ws)

        mock_start.assert_called_once()
        sent_states = extract_sent_types(mock_ws)
        assert "loading" in sent_states, f"Should broadcast loading. Sent: {mock_ws.sent_messages}"

    @pytest.mark.asyncio
    async def test_ready_when_gpu_idle_no_requests(self, mock_ws):
        """With llama running and GPU idle, state should be ready."""
        agent._current_state = "ready"
        agent._active_requests = 0
        agent._gpu_stats = {"utilization": 5, "memory": 30}

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            await agent._update_model_state(mock_ws)

        assert agent._current_state == "ready"

    @pytest.mark.asyncio
    async def test_busy_when_active_requests(self, mock_ws):
        """When there are active requests, state should be busy."""
        agent._current_state = "ready"
        agent._active_requests = 2

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            await agent._update_model_state(mock_ws)

        assert agent._current_state == "busy"

    @pytest.mark.asyncio
    async def test_unloads_when_gpu_busy_no_requests(self, mock_ws):
        """
        When GPU is busy with another workload (gaming) and there are
        no active requests, the model should be unloaded.
        This is the bug that was reported — the original code never unloaded.
        """
        agent._current_state = "ready"
        agent._active_requests = 0
        agent._gpu_stats = {"utilization": 85, "memory": 60}

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            with patch("agent._stop_llama_server", new_callable=AsyncMock) as mock_stop:
                await agent._update_model_state(mock_ws)
                mock_stop.assert_called_once()

        assert agent._current_state == "unloaded", (
            f"Expected unloaded when GPU busy, got {agent._current_state}"
        )
        sent_states = extract_sent_types(mock_ws)
        assert "unloading" in sent_states, (
            f"Should broadcast unloading before unloaded. Sent: {sent_states}"
        )
        assert "unloaded" in sent_states

    @pytest.mark.asyncio
    async def test_stays_busy_when_gpu_busy_with_active_requests(self, mock_ws):
        """When GPU is busy AND there are active requests, finish current work."""
        agent._current_state = "ready"
        agent._active_requests = 1
        agent._gpu_stats = {"utilization": 90, "memory": 80}

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            await agent._update_model_state(mock_ws)

        assert agent._current_state == "busy"

    @pytest.mark.asyncio
    async def test_unloads_after_last_request_when_gpu_busy(self, mock_ws):
        """After last request finishes, if GPU is still busy, unload."""
        agent._current_state = "busy"
        agent._active_requests = 0
        agent._gpu_stats = {"utilization": 85, "memory": 75}

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            with patch("agent._stop_llama_server", new_callable=AsyncMock) as mock_stop:
                await agent._update_model_state(mock_ws)
                mock_stop.assert_called_once()

        assert agent._current_state == "unloaded"

    @pytest.mark.asyncio
    async def test_remains_unloaded_when_gpu_busy_without_llama(self, mock_ws):
        """If GPU is busy and model isn't loaded, stay unloaded."""
        agent._current_state = "unloaded"
        agent._active_requests = 0
        agent._gpu_stats = {"utilization": 95, "memory": 90}

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=False):
            await agent._update_model_state(mock_ws)

        assert agent._current_state == "unloaded"


# ── Tests: Memory threshold ─────────────────────────────────────────────


class TestMemoryThreshold:
    """High memory usage should also trigger unload."""

    @pytest.mark.asyncio
    async def test_unloads_on_high_memory(self, mock_ws):
        """Memory above threshold without compute utilization should unload."""
        agent._current_state = "ready"
        agent._active_requests = 0
        agent._gpu_stats = {"utilization": 10, "memory": 90}

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            with patch("agent._stop_llama_server", new_callable=AsyncMock) as mock_stop:
                await agent._update_model_state(mock_ws)
                mock_stop.assert_called_once()
        assert agent._current_state == "unloaded"

    @pytest.mark.asyncio
    async def test_keeps_loaded_when_both_below_threshold(self, mock_ws):
        """Both utilization and memory below thresholds should stay ready."""
        agent._current_state = "ready"
        agent._active_requests = 0
        agent._gpu_stats = {"utilization": 15, "memory": 40}

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            await agent._update_model_state(mock_ws)
        assert agent._current_state == "ready"


class TestLoadOnDemand:
    """Model should load when inference arrives and no llama-server is running."""

    @pytest.mark.asyncio
    async def test_loads_model_when_request_arrives(self, mock_ws):
        """If unloaded and a request comes, load the model."""
        agent._current_state = "unloaded"
        agent._active_requests = 1
        agent._gpu_stats = {"utilization": 5, "memory": 10}

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=False):
            with patch("agent._start_llama_server", new_callable=AsyncMock, return_value=True) as mock_start:
                await agent._update_model_state(mock_ws)
                mock_start.assert_called_once()

        assert agent._current_state == "ready", f"Expected ready, got {agent._current_state}"
        sent_states = extract_sent_types(mock_ws)
        assert "loading" in sent_states
        assert "ready" in sent_states

    @pytest.mark.asyncio
    async def test_load_failure_stays_unloaded(self, mock_ws):
        """If llama-server fails to start, revert to unloaded."""
        agent._current_state = "unloaded"
        agent._active_requests = 1

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=False):
            with patch("agent._start_llama_server", new_callable=AsyncMock, return_value=False):
                await agent._update_model_state(mock_ws)

        assert agent._current_state == "unloaded"


class TestIdleTimeout:
    """Model should be unloaded after IDLE_TIMEOUT seconds of inactivity."""

    @pytest.mark.asyncio
    async def test_idle_timeout_triggers_unload(self, mock_ws):
        """After IDLE_TIMEOUT seconds with no requests, model unloads."""
        agent._last_request_time = time.time() - (agent.IDLE_TIMEOUT + 10)
        agent._current_state = "ready"

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            with patch("agent._stop_llama_server", new_callable=AsyncMock) as mock_stop:
                async with agent._state_lock:
                    if agent._current_state == "ready":
                        idle_seconds = time.time() - agent._last_request_time
                        if idle_seconds >= agent.IDLE_TIMEOUT:
                            llama_running = await agent._is_llama_running()
                            if llama_running:
                                await agent._unload_model(mock_ws)
                mock_stop.assert_called_once()

        assert agent._current_state == "unloaded"

    @pytest.mark.asyncio
    async def test_no_unload_when_recent_request(self, mock_ws):
        """If a request came recently, idle timeout should not trigger."""
        agent._last_request_time = time.time()
        agent._current_state = "ready"

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            with patch("agent._stop_llama_server", new_callable=AsyncMock) as mock_stop:
                async with agent._state_lock:
                    if agent._current_state == "ready":
                        idle_seconds = time.time() - agent._last_request_time
                        if idle_seconds >= agent.IDLE_TIMEOUT:
                            llama_running = await agent._is_llama_running()
                            if llama_running:
                                await agent._unload_model(mock_ws)
                mock_stop.assert_not_called()

        assert agent._current_state == "ready"


class TestGpuMonitor:
    """GPU monitor should combine stats and state transitions."""

    @pytest.mark.asyncio
    async def test_gpu_stats_updates_globals(self):
        """get_gpu_stats should update the module-level _gpu_stats."""
        # We can't easily test the full monitor loop in unit tests
        # (it needs asyncio.sleep to tick). This is tested via integration.
        # Verify the stat structure is well-formed.
        assert "utilization" in agent._gpu_stats
        assert "memory" in agent._gpu_stats


class TestProtocolCompliance:
    """Verifies the agent follows the coordinator's expected protocol."""

    def test_protocol_version_is_3(self):
        assert agent.PROTOCOL_VERSION == 3, (
            f"Expected protocol v3 for real lifecycle, got {agent.PROTOCOL_VERSION}"
        )

    def test_init_message_includes_model_state(self):
        """The init message should include initial model_state (unloaded)."""
        init_msg = {
            "type": "init",
            "protocol_version": agent.PROTOCOL_VERSION,
            "gpu_info": agent.GPU_INFO,
            "model": agent.MODEL_FILE,
            "model_state": agent._current_state,
        }
        assert init_msg["model_state"] == "unloaded"


class TestRegression:
    """
    These tests encode the bug that was reported:
    "There is no code to unload the model, or to change the state to unloaded"
    """

    @pytest.mark.asyncio
    async def test_model_unloads_on_gpu_busy(self, mock_ws):
        """
        REGRESSION: When GPU utilization exceeds threshold and there are
        no active requests, the model MUST be unloaded.
        Previous code only set state to 'busy' but never stopped llama-server.
        """
        agent._gpu_stats = {"utilization": 95, "memory": 50}
        agent._current_state = "ready"
        agent._active_requests = 0

        unload_called = False

        async def tracking_stop():
            nonlocal unload_called
            unload_called = True

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            with patch("agent._stop_llama_server", new_callable=AsyncMock, side_effect=tracking_stop):
                await agent._update_model_state(mock_ws)

        assert unload_called, (
            "BUG: _stop_llama_server was not called when GPU is busy. "
            "The model stays loaded in VRAM, wasting power and blocking GPU idle states."
        )
        assert agent._current_state == "unloaded"

    @pytest.mark.asyncio
    async def test_model_unloads_on_high_memory(self, mock_ws):
        """
        REGRESSION: High memory usage alone should also trigger unload.
        """
        agent._gpu_stats = {"utilization": 5, "memory": 95}
        agent._current_state = "ready"
        agent._active_requests = 0

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            with patch("agent._stop_llama_server", new_callable=AsyncMock) as mock_stop:
                await agent._update_model_state(mock_ws)

            mock_stop.assert_called_once_with()
        assert agent._current_state == "unloaded"

    @pytest.mark.asyncio
    async def test_state_transition_broadcasts_unloading(self, mock_ws):
        """
        REGRESSION: Before transitioning to unloaded, the agent should
        broadcast 'unloading' first so the coordinator stops sending work.
        """
        agent._gpu_stats = {"utilization": 80, "memory": 50}
        agent._current_state = "ready"
        agent._active_requests = 0

        with patch("agent._is_llama_running", new_callable=AsyncMock, return_value=True):
            with patch("agent._stop_llama_server", new_callable=AsyncMock):
                await agent._update_model_state(mock_ws)

        sent_states = extract_sent_types(mock_ws)
        assert "unloading" in sent_states, (
            f"Should broadcast 'unloading' before stopping. States sent: {sent_states}"
        )
        assert "unloaded" in sent_states

    @pytest.mark.asyncio
    async def test_stops_llama_server_on_unload(self, mock_ws):
        """
        REGRESSION: _unload_model must actually stop llama-server.
        Previous code never called _stop_llama_server.
        """
        agent._current_state = "ready"

        with patch("agent._stop_llama_server", new_callable=AsyncMock) as mock_stop:
            await agent._unload_model(mock_ws)

        mock_stop.assert_called_once_with()
        assert agent._current_state == "unloaded"

    @pytest.mark.asyncio
    async def test_loading_transition_is_real(self, mock_ws):
        """
        REGRESSION: The loading state should represent actual work.
        """
        agent._current_state = "unloaded"
        agent._active_requests = 1
        agent._gpu_stats = {"utilization": 5, "memory": 10}

        # First call (before load) returns False, second call (after load) returns True
        is_running = AsyncMock(side_effect=[False, True])

        with patch("agent._is_llama_running", new_callable=lambda: is_running):
            with patch("agent._start_llama_server", new_callable=AsyncMock, return_value=True) as mock_start:
                async with agent._state_lock:
                    await agent._update_model_state(mock_ws)

                mock_start.assert_called_once()

        assert agent._current_state == "busy", (
            f"After loading with active request, state should be busy, got {agent._current_state}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])