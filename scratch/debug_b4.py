import asyncio
import json
import pathlib
import sys

_REPO_ROOT = pathlib.Path("/Users/davidkitdave/Projects/Ledgr-QBS")
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types as genai_types
from accounting_agents.chat_eval.agent import app as chat_eval_app

async def main():
    eval_case_id = "B4_chat_summarize_by_category_trajectory"
    eval_set_path = _REPO_ROOT / "tests/eval/datasets/ledgr.evalset.json"
    
    raw = json.loads(eval_set_path.read_text())
    raw_case = next(c for c in raw["eval_cases"] if c["eval_id"] == eval_case_id)
    session_input = raw_case.get("session_input") or {}
    
    session_service = InMemorySessionService()
    runner = Runner(app=chat_eval_app, session_service=session_service)
    session = await session_service.create_session(
        app_name=chat_eval_app.name,
        user_id=session_input.get("user_id", "eval_user"),
        state=session_input.get("state") or {},
    )
    
    user_content = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text="total purchases this FY?")],
    )
    
    print("Running B4 inference...")
    async for event in runner.run_async(
        user_id=session.user_id,
        session_id=session.id,
        new_message=user_content,
    ):
        print(f"EVENT: {event}")

if __name__ == "__main__":
    asyncio.run(main())
