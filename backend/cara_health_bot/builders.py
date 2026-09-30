"""[DEPRECATED] Amazon Lex V2 Bot & IAM Builders (Quarantined in Phase 2).

This module has been quarantined and replaced by the Pipecat + Amazon Bedrock
Nova 2 Sonic real-time streaming pipeline. The historical builder implementations
are preserved in `backend/legacy/builders.py`.
"""

from __future__ import annotations

import warnings
from typing import Any

# Issue runtime deprecation warning when imported
warnings.warn(
    "cara_health_bot.builders is deprecated as of Phase 2. "
    "Lex V2 bot generation has been replaced by Pipecat + Nova 2 Sonic streaming.",
    DeprecationWarning,
    stacklevel=2,
)

# Re-export from legacy archive for backward compatibility in existing unit test suites
from legacy.builders import *  # noqa: F401, F403