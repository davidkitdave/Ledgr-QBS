"""Ledgr Coordinator -- the front-desk agent.

A lean ADK ``LlmAgent`` that reads each user message, decides intent, and
dispatches to the existing deterministic engine via tools. It intentionally does
NOT contain the legacy Acting / Investigation / ALF machinery in
``invoice_processing/agent.py`` -- that solves a different problem (learned
invoice-approval compliance rules) and is not used here.

Run locally:
    adk web .                          # then pick "ledgr_coordinator"
    agents-cli run "what can you do?"  # after the project is enhanced for agents-cli

Model and backend follow the project convention (``GEMINI_FLASH_MODEL`` env var;
``GOOGLE_GENAI_USE_VERTEXAI`` selects AI Studio vs Vertex).
"""

from __future__ import annotations

import os

from google.adk.agents import LlmAgent
from google.genai import types

from .prompt import COORDINATOR_INSTRUCTION
from .tools import capabilities, inspect_document, process_documents

MODEL = os.getenv("GEMINI_FLASH_MODEL", "gemini-2.5-flash")

root_agent = LlmAgent(
    name="ledgr_coordinator",
    model=MODEL,
    generate_content_config=types.GenerateContentConfig(temperature=0),
    instruction=COORDINATOR_INSTRUCTION,
    tools=[capabilities, inspect_document, process_documents],
)

from google.adk.apps import App

app = App(root_agent=root_agent, name="ledgr_coordinator")
