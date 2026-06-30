"""Standalone extraction agent used as the GEPA optimization target."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from ledgr_agent.internal.gemini import lite_model
from ledgr_agent.internal.schemas import BUNDLE_READER_INSTRUCTION, READ_PROMPT, ReadDocumentBundle

root_agent = LlmAgent(
    name="extraction_agent",
    model=lite_model(),
    instruction=READ_PROMPT + "\n\n" + BUNDLE_READER_INSTRUCTION,
    output_schema=ReadDocumentBundle,
    generate_content_config=types.GenerateContentConfig(temperature=0),
    description="Reference-free financial document extraction agent (GEPA optimization target).",
)
