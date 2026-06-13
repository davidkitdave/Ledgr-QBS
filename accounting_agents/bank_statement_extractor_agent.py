from pydantic import BaseModel, Field
from google.adk.workflow import Workflow, node
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.genai import types
import io
import openpyxl
from . import config
from .tools import _dynamic_format_mapping

class Transaction(BaseModel):
    date: str = Field(description="Date of the transaction")
    description: str = Field(description="Transaction description")
    amount: float = Field(description="Transaction amount (negative for withdrawals)")

class BankAccount(BaseModel):
    account_uuid: str = Field(description="Last 4 digits of the account number")
    transactions: list[Transaction] = Field(description="List of transactions for this account")

class BankStatementData(BaseModel):
    accounts: list[BankAccount] = Field(description="List of bank accounts found in the statement")

statement_extractor = LlmAgent(
    name="statement_extractor",
    model=config.MODEL_STD,
    instruction=(
        "You are an Intelligent Document Processing agent specializing in Bank Statements. "
        "Extract all distinct bank accounts and their transactions from the document. "
        "Return the exact data structured according to the schema."
    ),
    output_schema=BankStatementData
)

@node
async def process_and_save_statement(ctx: Context, node_input: BankStatementData):
    target_headers = ctx.state.get("user:bank_target_headers", "Date, Description, Amount, AccountCode")
    
    wb = openpyxl.Workbook()
    wb.remove(wb.active) # Remove default sheet
    
    for account in node_input.accounts:
        ws = wb.create_sheet(title=f"Account_{account.account_uuid}")
        
        # Convert pydantic transactions to dicts for the mapping tool
        tx_dicts = [tx.model_dump() for tx in account.transactions]
        mapped_headers, mapped_rows = _dynamic_format_mapping(tx_dicts, target_headers)
        
        ws.append(mapped_headers)
        for row in mapped_rows:
            ws.append(row)
            
    file_stream = io.BytesIO()
    wb.save(file_stream)
    file_stream.seek(0)
    
    # --- PHYSICAL FOLDER SAVE ---
    import os
    from datetime import datetime
    
    financial_year = ctx.state.get("user:fye_month", "UnknownFY")
    client_id = ctx.state.get("user:client_name", ctx.session.id)
    
    month_folder = datetime.now().strftime("%Y-%m")
    local_dir = os.path.join("client_data", client_id, financial_year, "bank_statements", month_folder)
    os.makedirs(local_dir, exist_ok=True)
    
    timestamp = datetime.now().strftime("%H%M%S")
    local_filename = os.path.join(local_dir, f"statement_{timestamp}.xlsx")
    
    with open(local_filename, "wb") as f:
        f.write(file_stream.getvalue())
    # ----------------------------
    
    blob = types.Blob(mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", data=file_stream.read())
    part = types.Part(inline_data=blob)
    
    message = f"Successfully processed Bank Statement across {len(node_input.accounts)} accounts. Saved to physical folder: `{local_filename}` and attached below."
    text_part = types.Part.from_text(text=message)
    
    content = types.Content(role="model", parts=[text_part, part])
    yield Event(content=content, output=message)

bank_statement_extractor_agent = Workflow(
    name="bank_statement_extractor_agent",
    description="Intelligent Document Processing (IDP) agent for raw Bank Statement PDFs. Extracts multi-currency, multi-account data.",
    edges=[
        ('START', statement_extractor),
        (statement_extractor, process_and_save_statement)
    ]
)
