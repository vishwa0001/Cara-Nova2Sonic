#!/usr/bin/env python3
"""Pre-warming script for Cara Health Bot cold-start elimination.

Warms all cold-startable components in parallel before campaign execution:
1. cara-health-bot-agent-availability Lambda (invoked N times in parallel)
2. Lex V2 Identity Bot (recognize_text)
3. Lex V2 Coaching Bot - Nova 2 Sonic (recognize_text)
4. Lex V2 Availability Bot (recognize_text)
5. Amazon Q in Connect / Bedrock Nova Lite (session ping & inference warm-up)

Safe to run repeatedly — no side effects on real customer records or contacts.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import BotoCoreError, ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cara_health_bot.config import load_config


@dataclass
class PrewarmResult:
    component: str
    target: str
    status: str
    duration_ms: float
    details: str = ""

    def __str__(self) -> str:
        badge = "[OK]    " if self.status == "SUCCESS" else ("[SKIP]  " if self.status == "SKIPPED" else "[FAIL]  ")
        return f"{badge} {self.component:32s} | {self.target:35s} | {self.duration_ms:6.1f} ms | {self.details}"


def _get_aws_session(profile: str | None = None, region: str = "us-east-1") -> boto3.Session:
    prof = profile or os.environ.get("AWS_PROFILE", "careharmony-main")
    try:
        return boto3.Session(profile_name=prof, region_name=region)
    except Exception:
        return boto3.Session(region_name=region)


def _load_deployed_state() -> dict[str, Any]:
    state_file = ROOT / "deployment-state.json"
    if state_file.is_file():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return data.get("resources") or data.get("outputs") or data
        except Exception:
            pass
    return {}


def prewarm_lambda(
    session: boto3.Session,
    function_name: str,
    worker_idx: int = 1,
    qualifier: str | None = None,
    dry_run: bool = False,
) -> PrewarmResult:
    """Pre-warm an individual Lambda worker by sending a no-op ping payload."""
    t0 = time.perf_counter()
    target_desc = f"{function_name}:{qualifier}" if qualifier else function_name
    if dry_run:
        return PrewarmResult(
            component=f"Lambda (Worker {worker_idx})",
            target=target_desc,
            status="SUCCESS",
            duration_ms=0.0,
            details="Dry run simulated",
        )

    lambda_client = session.client("lambda")
    ping_payload = {
        "Details": {
            "Parameters": {"operation": "ping"},
            "ContactData": {"ContactId": f"prewarm-ping-{uuid.uuid4().hex[:8]}", "Attributes": {}},
        },
        "operation": "ping",
    }
    kwargs: dict[str, Any] = {
        "FunctionName": function_name,
        "InvocationType": "RequestResponse",
        "Payload": json.dumps(ping_payload),
    }
    if qualifier:
        kwargs["Qualifier"] = qualifier

    try:
        response = lambda_client.invoke(**kwargs)
        t1 = time.perf_counter()
        dur = (t1 - t0) * 1000
        payload_raw = response["Payload"].read().decode("utf-8") if "Payload" in response else ""
        return PrewarmResult(
            component=f"Lambda (Worker {worker_idx})",
            target=target_desc,
            status="SUCCESS",
            duration_ms=dur,
            details=f"Status {response.get('StatusCode', 200)}: {payload_raw[:60]}",
        )
    except Exception as e:
        t1 = time.perf_counter()
        dur = (t1 - t0) * 1000
        return PrewarmResult(
            component=f"Lambda (Worker {worker_idx})",
            target=target_desc,
            status="FAILED",
            duration_ms=dur,
            details=str(e),
        )


def prewarm_lex(
    session: boto3.Session,
    component_name: str,
    bot_id: str,
    alias_id: str,
    locale_id: str = "en_US",
    dry_run: bool = False,
) -> PrewarmResult:
    """Pre-warm a Lex V2 bot container and NLU/ASR backend via RecognizeText."""
    t0 = time.perf_counter()
    target_desc = f"Bot:{bot_id} Alias:{alias_id}"
    if dry_run:
        return PrewarmResult(
            component=component_name,
            target=target_desc,
            status="SUCCESS",
            duration_ms=0.0,
            details="Dry run simulated",
        )

    if not bot_id or not alias_id:
        return PrewarmResult(
            component=component_name,
            target=target_desc,
            status="SKIPPED",
            duration_ms=0.0,
            details="Bot ID or Alias ID not configured",
        )

    lex_runtime = session.client("lexv2-runtime")
    session_id = f"prewarm-{uuid.uuid4().hex[:8]}"
    try:
        resp = lex_runtime.recognize_text(
            botId=bot_id,
            botAliasId=alias_id,
            localeId=locale_id,
            sessionId=session_id,
            text="hello",
        )
        t1 = time.perf_counter()
        dur = (t1 - t0) * 1000
        top_intent = (resp.get("sessionState") or {}).get("intent", {}).get("name", "None")
        return PrewarmResult(
            component=component_name,
            target=target_desc,
            status="SUCCESS",
            duration_ms=dur,
            details=f"Intent: {top_intent} (Session {session_id})",
        )
    except Exception as e:
        t1 = time.perf_counter()
        dur = (t1 - t0) * 1000
        return PrewarmResult(
            component=component_name,
            target=target_desc,
            status="FAILED",
            duration_ms=dur,
            details=str(e),
        )


def prewarm_bedrock_nova_lite(
    session: boto3.Session,
    orchestration_model_id: str = "us.amazon.nova-lite-v1:0",
    dry_run: bool = False,
) -> PrewarmResult:
    """Pre-warm Bedrock Nova Lite cross-region inference container directly via converse.

    AWS Wisdom/QConnect does not provide a delete_session API, and creating dummy
    sessions does not invoke LLM inference. Warming Bedrock directly eliminates
    cold starts on Nova Lite without accumulating orphaned Wisdom sessions.
    """
    t0 = time.perf_counter()
    target_desc = f"Model:{orchestration_model_id}"
    if dry_run:
        return PrewarmResult(
            component="Bedrock Nova Lite (Q Orchestrator)",
            target=target_desc,
            status="SUCCESS",
            duration_ms=0.0,
            details="Dry run simulated",
        )

    notes = []
    try:
        bedrock = session.client("bedrock-runtime")
        resp = bedrock.converse(
            modelId=orchestration_model_id,
            messages=[{"role": "user", "content": [{"text": "ping"}]}],
            inferenceConfig={"maxTokens": 1, "temperature": 0.0},
        )
        t_model = (time.perf_counter() - t0) * 1000
        stop_reason = resp.get("stopReason", "end")
        notes.append(f"Bedrock TTFT: {t_model:.0f}ms ({stop_reason})")
    except Exception as e:
        notes.append(f"Bedrock ping: {type(e).__name__}")

    t1 = time.perf_counter()
    dur = (t1 - t0) * 1000
    details_str = " | ".join(notes) if notes else "No direct warm endpoints reached"
    return PrewarmResult(
        component="Bedrock Nova Lite (Q Orchestrator)",
        target=target_desc,
        status="SUCCESS" if "Bedrock TTFT" in details_str else "SKIPPED",
        duration_ms=dur,
        details=details_str,
    )


# Alias for backwards compatibility with tests / earlier callers
prewarm_wisdom_and_bedrock = prewarm_bedrock_nova_lite


def run_prewarm(
    lead_time_seconds: int = 60,
    concurrency: int = 3,
    profile: str | None = None,
    region: str = "us-east-1",
    dry_run: bool = False,
) -> list[PrewarmResult]:
    """Execute all pre-warming tasks in parallel and return the aggregated results."""
    session = _get_aws_session(profile=profile, region=region)
    state = _load_deployed_state()
    try:
        cfg = load_config()
    except Exception:
        cfg = None

    identity_bot_id = state.get("identityBotId") or "4M8I8HGPND"
    identity_alias_id = state.get("identityBotAliasId") or "KJZH9WFPLC"
    availability_bot_id = state.get("availabilityBotId") or "TZ7RVZJ2VG"
    availability_alias_id = state.get("availabilityBotAliasId") or "YS2ECNTEXL"
    coaching_bot_id = state.get("botId") or "4S3WG7D9ZQ"
    coaching_alias_id = state.get("botAliasId") or "TZSUXEY14O"
    assistant_id = state.get("assistantId") or "43ccf678-f5cf-4f74-ab20-2d9f8e2fade8"
    orchestration_model = getattr(cfg, "orchestration_model_id", "us.amazon.nova-lite-v1:0") if cfg else "us.amazon.nova-lite-v1:0"

    print("=" * 90)
    print(f"CARA HEALTH BOT INFRASTRUCTURE PRE-WARMING ({lead_time_seconds}s before campaign dial)")
    print(f"Region: {region} | Concurrency: {concurrency} | Dry Run: {dry_run}")
    print("=" * 90)

    start_wall = time.perf_counter()
    results: list[PrewarmResult] = []

    with ThreadPoolExecutor(max_workers=concurrency + 4) as executor:
        futures = []

        # 1. Warm unprovisioned agent-availability Lambda (N concurrent instances)
        avail_func_name = state.get("availability_function") or "cara-health-bot-agent-availability"
        for i in range(1, concurrency + 1):
            futures.append(
                executor.submit(
                    prewarm_lambda,
                    session=session,
                    function_name=avail_func_name,
                    worker_idx=i,
                    dry_run=dry_run,
                )
            )

        # 2. Warm Lex V2 Identity Bot
        futures.append(
            executor.submit(
                prewarm_lex,
                session=session,
                component_name="Lex Identity Bot",
                bot_id=identity_bot_id,
                alias_id=identity_alias_id,
                locale_id=getattr(cfg, "locale", "en_US") if cfg else "en_US",
                dry_run=dry_run,
            )
        )

        # 3. Warm Lex V2 Availability Bot
        futures.append(
            executor.submit(
                prewarm_lex,
                session=session,
                component_name="Lex Availability Bot",
                bot_id=availability_bot_id,
                alias_id=availability_alias_id,
                locale_id=getattr(cfg, "locale", "en_US") if cfg else "en_US",
                dry_run=dry_run,
            )
        )

        # 4. Warm Lex V2 Coaching Bot (Nova 2 Sonic)
        futures.append(
            executor.submit(
                prewarm_lex,
                session=session,
                component_name="Lex Coaching Bot (Nova 2 Sonic)",
                bot_id=coaching_bot_id,
                alias_id=coaching_alias_id,
                locale_id=getattr(cfg, "locale", "en_US") if cfg else "en_US",
                dry_run=dry_run,
            )
        )

        # 5. Warm Bedrock Nova Lite (Q in Connect Orchestration Model)
        futures.append(
            executor.submit(
                prewarm_bedrock_nova_lite,
                session=session,
                orchestration_model_id=orchestration_model,
                dry_run=dry_run,
            )
        )

        for future in as_completed(futures):
            res = future.result()
            results.append(res)
            print(res)

    total_wall_ms = (time.perf_counter() - start_wall) * 1000
    print("-" * 90)
    print(f"Pre-warming completed in {total_wall_ms:.1f} ms (all components warmed concurrently in parallel)")
    print("=" * 90)
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pre-warm Cara Health Bot Lambdas, Lex bots, and Bedrock models before scheduled campaign dials."
    )
    parser.add_argument(
        "--lead-time",
        type=int,
        default=60,
        help="Lead time in seconds before campaign start (default: 60)",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Number of concurrent Lambda workers to warm (default: 3)",
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region (default: us-east-1)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS named profile to use (default: careharmony-main or AWS_PROFILE)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate pre-warming without making real AWS API calls",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    results = run_prewarm(
        lead_time_seconds=args.lead_time,
        concurrency=args.concurrency,
        profile=args.profile,
        region=args.region,
        dry_run=args.dry_run,
    )
    failed = [r for r in results if r.status == "FAILED"]
    if failed:
        print(f"\n[WARN] {len(failed)} component(s) encountered errors during pre-warm.")
        sys.exit(1)


if __name__ == "__main__":
    main()
