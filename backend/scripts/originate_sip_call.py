import asyncio
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from gateway.sip_transport import SIPRTPMediaServer, SIPSignalingServer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("sip_dialer")


async def main():
    destination = "+15822671755"
    source = "+17134811560"
    vc_host = "cafawc0zxzbcpxusmsonrd.voiceconnector.chime.aws"
    public_ip = "100.49.33.242"

    logger.info(f"Starting SIP/RTP Media Manager in ECHO MODE (Port range: 10000-20000)...")
    media_server = SIPRTPMediaServer(host="0.0.0.0", port=10000, echo_mode=True)
    await media_server.start()

    logger.info(f"Starting SIP Signaling Server on UDP 5060 (Public IP: {public_ip})...")
    signaling_server = SIPSignalingServer(
        host="0.0.0.0",
        port=5060,
        public_ip=public_ip,
        media_server=media_server,
    )
    await signaling_server.start()

    logger.info(f"Originating direct SIP call to {destination} via {vc_host}...")
    call_id = await signaling_server.originate_call(
        destination_phone=destination,
        source_phone=source,
        vc_host=vc_host,
    )

    logger.info(f"Call initiated with Call-ID: {call_id}. Waiting for response / call session...")
    try:
        # Keep alive for 60s to handle active test call
        await asyncio.sleep(60)
    finally:
        logger.info("Cleaning up SIP/RTP sessions...")
        signaling_server.stop()
        media_server.stop()


if __name__ == "__main__":
    asyncio.run(main())
