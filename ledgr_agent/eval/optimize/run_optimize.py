#!/usr/bin/env python3
"""Programmatic GEPA runner for the ledgr extraction prompt.

Run only after eval matrix + schema field descriptions pass baseline.
GEPA optimizes READ_PROMPT + BUNDLE_READER_INSTRUCTION only — not Pydantic
field descriptions (see ADR-0035).
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import shutil
import tempfile

from google.adk.evaluation.eval_config import EvalConfig
from google.adk.evaluation.local_eval_sets_manager import LocalEvalSetsManager
from google.adk.optimization.gepa_root_agent_prompt_optimizer import GEPARootAgentPromptOptimizer
from google.adk.optimization.local_eval_sampler import LocalEvalSampler, LocalEvalSamplerConfig

from ledgr_agent.eval.optimize import extraction_agent
from ledgr_agent.eval.register_custom_metrics import register_ledgr_light_custom_metrics

_REPO = pathlib.Path(__file__).resolve().parents[3]
_EVALSET_SRC = _REPO / "ledgr_agent" / "eval" / "datasets" / "ledgr_light.evalset.json"
_EVAL_CONFIG = _REPO / "ledgr_agent" / "eval" / "eval_config_ledgr_light_gepa.json"
_EVAL_SET_ID = "ledgr_light_v1"
_APP_NAME = "ledgr_agent"


def _faithfulness_only_config() -> EvalConfig:
    raw = json.loads(_EVAL_CONFIG.read_text(encoding="utf-8"))
    return EvalConfig.model_validate(raw)


def _materialize_evalset(tmp_agents_dir: pathlib.Path) -> LocalEvalSetsManager:
    app_dir = tmp_agents_dir / _APP_NAME
    app_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(_EVALSET_SRC, app_dir / f"{_EVAL_SET_ID}.evalset.json")
    return LocalEvalSetsManager(agents_dir=str(tmp_agents_dir))


async def _run() -> str:
    eval_config = _faithfulness_only_config()
    register_ledgr_light_custom_metrics(eval_config)

    with tempfile.TemporaryDirectory() as tmp:
        agents_dir = pathlib.Path(tmp)
        sets_manager = _materialize_evalset(agents_dir)
        sampler_config = LocalEvalSamplerConfig(
            eval_config=eval_config,
            app_name=_APP_NAME,
            train_eval_set=_EVAL_SET_ID,
        )
        sampler = LocalEvalSampler(config=sampler_config, eval_sets_manager=sets_manager)
        optimizer = GEPARootAgentPromptOptimizer()
        result = await optimizer.optimize(extraction_agent.root_agent, sampler)
        best = result.optimized_agents[0]
        return str(best.optimized_agent.instruction)


def main() -> None:
    instruction = asyncio.run(_run())
    print("=== Optimized instruction ===")
    print(instruction)


if __name__ == "__main__":
    main()
