from google.adk.agents import Agent
from google.adk.models import Gemini
from . import config
from .tools import request_human_approval, categorize_transaction, create_journal_entry

fincot_blueprint = """
### FinCoT (Financial Chain-of-Thought) Blueprint

Follow this strict step-by-step methodology for bank feed classification:
```mermaid
graph TD
    A[Receive Bank Transaction] --> B{Verify Amount Units}
    B --> C[Analyze Vendor/Description]
    C --> D[Determine Accounting Category]
    D --> E[Draft Journal Entry]
    E --> F[Double-Entry Check: Debits = Credits]
    F --> G[Post Journal Entry]
```
Ensure you always perform double-entry checks and explicitly state the debit and credit accounts.
"""

bank_feed_generator = Agent(
    name="bank_feed_generator",
    model=Gemini(model=config.MODEL_LITE),
    description="Classifies bank feed transactions and drafts double-entry journal entries.",
    instruction=(
        "You are an expert bookkeeping agent and strict digital auditor. "
        "Your task is to classify bank transactions, draft double-entry journal entries, and post them in a single pass.\n\n"
        f"{fincot_blueprint}\n\n"
        
        "Step 1: Classification\n"
        "Use the `categorize_transaction` tool to determine the correct GL account based on the transaction description.\n\n"
        
        "Step 2: Double-Entry Draft\n"
        "Draft the journal entry. Verify mathematically that Debits equal Credits.\n\n"
        
        "Step 3: Post or Request Approval\n"
        "If the GL account is clear and the math is correct, use the `create_journal_entry` tool to finalize the posting immediately. "
        "If the category is 'Uncategorized Expense' or the transaction is ambiguous, you MUST use the `request_human_approval` tool to get sign-off from the accountant instead of posting."
    ),
    tools=[categorize_transaction, create_journal_entry, request_human_approval]
)
