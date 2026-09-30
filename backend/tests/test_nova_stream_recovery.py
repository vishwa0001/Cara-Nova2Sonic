"""Nova Sonic sometimes fails a new stream with "The system encountered an
unexpected error" right after the system prompt. The gateway used to just
stop reading, leaving the patient on a silent line (call 0e40635e, 97s).
It must reopen the stream, and hang up if the model stays unavailable."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from backend.gateway import nova_sonic_service
from backend.gateway.nova_sonic_service import NovaSonicSession


class _FailingStream:
    def __init__(self):
        self.input_stream = SimpleNamespace(send=AsyncMock(), close=AsyncMock())

    async def await_output(self):
        async def _gen():
            raise RuntimeError("The system encountered an unexpected error during processing.")
            yield  # pragma: no cover
        return None, _gen()


def _session():
    s = NovaSonicSession(MagicMock(), contact_id="0e40635e", on_hangup=AsyncMock())
    s.is_active = True
    s.stream = _FailingStream()
    return s


class StreamRecoveryTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(nova_sonic_service, "STREAM_RESTART_DELAY_SECONDS", 0)
        p.start()
        self.addCleanup(p.stop)

    def test_reopens_stream_when_it_fails_before_cara_speaks(self):
        s = _session()

        async def _start_ok():
            s.is_active = True
        s.start = AsyncMock(side_effect=_start_ok)
        asyncio.run(s._read_responses())
        s.start.assert_awaited_once()
        s.on_hangup.assert_not_awaited()
        self.assertTrue(s.is_active)

    def test_hangs_up_when_reopened_stream_keeps_failing(self):
        s = _session()

        async def _start_then_fail_again():
            s.is_active = True
            s.stream = _FailingStream()
            await s._read_responses()  # the new reader hits the same error
        s.start = AsyncMock(side_effect=_start_then_fail_again)
        asyncio.run(s._read_responses())
        self.assertEqual(s.start.await_count, nova_sonic_service.MAX_STREAM_RESTARTS)
        s.on_hangup.assert_awaited_once()

    def test_hangs_up_when_stream_cannot_be_reopened(self):
        s = _session()
        s.start = AsyncMock()  # start() fails: is_active stays False
        asyncio.run(s._read_responses())
        s.start.assert_awaited_once()
        s.on_hangup.assert_awaited_once()

    def test_hangs_up_without_restart_when_stream_dies_mid_conversation(self):
        s = _session()
        s._audio_out_bytes = 3200  # Cara already spoke
        s.start = AsyncMock()
        asyncio.run(s._read_responses())
        s.start.assert_not_awaited()
        s.on_hangup.assert_awaited_once()

    def test_no_recovery_when_session_was_ended_on_purpose(self):
        s = _session()
        s.is_active = False  # end() already ran
        s.start = AsyncMock()
        asyncio.run(s._read_responses())
        s.start.assert_not_awaited()
        s.on_hangup.assert_not_awaited()



class NovaRegionTests(unittest.TestCase):
    def test_nova_region_override(self):
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1", "NOVA_SONIC_REGION": "us-west-2"}):
            self.assertEqual(NovaSonicSession(MagicMock()).region_name, "us-west-2")

    def test_defaults_to_aws_region(self):
        with patch.dict("os.environ", {"AWS_REGION": "us-east-1"}, clear=False) as env:
            env.pop("NOVA_SONIC_REGION", None)
            self.assertEqual(NovaSonicSession(MagicMock()).region_name, "us-east-1")

if __name__ == "__main__":
    unittest.main()
