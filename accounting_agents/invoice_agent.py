from pydantic import BaseModel, Field
from google.adk.workflow import Workflow, node
from google.adk.agents import LlmAgent
from google.adk.agents.context import Context
from google.adk.events.event import Event
from google.genai import types
import io
import openpyxl
from . import config
from .tools import categorize_transaction, _dynamic_format_mapping

class LineItem(BaseModel):
    description: str = Field(description="Description of the item")
    amount: float = Field(description="Amount of the item")

class InvoiceData(BaseModel):
    vendor_name: str = Field(description="Name of the vendor or customer")
    date: str = Field(description="Date of the invoice")
    total_amount: float = Field(description="Total amount including tax")
    line_items: list[LineItem] = Field(description="List of line items")

invoice_extractor = LlmAgent(
    name="invoice_extractor",
    model=config.MODEL_LITE,
    instruction=(
        "You are an expert Invoice Processing Agent. "
        "Extract the required fields from the invoice PDF provided. "
        "Return the exact data structured according to the schema."
    ),
    output_schema=InvoiceData
)

@node
async def process_and_save_invoice(ctx: Context, node_input: InvoiceData):
    # Retrieve required config from state
    financial_year = ctx.state.get("user:fye_month", "UnknownFY")
    target_headers = ctx.state.get("user:invoice_target_headers", "Date, Vendor Name, Total Amount, GL Account")
    # For client_id, we can fallback to session id
    client_id = ctx.state.get("user:client_name", ctx.session.id)
    
    formatted_data = []
    
    # Categorize line items using the python function directly
    for item in node_input.line_items:
        gl_account = categorize_transaction(item.description, item.amount)
        formatted_data.append({
            "Date": node_input.date,
            "Vendor Name": node_input.vendor_name,
            "Description": item.description,
            "Total Amount": item.amount,
            "GL Account": gl_account
        })
        
    # Dynamically map the data
    mapped_headers, mapped_rows = _dynamic_format_mapping(formatted_data, target_headers)
    
    # Generate Excel file
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Invoice Data"
    ws.append(mapped_headers)
    for row in mapped_rows:
        ws.append(row)
        
    file_stream = io.BytesIO()
    wb.save(file_stream)
    file_stream.seek(0)
    
    # --- PHYSICAL FOLDER SAVE ---
    import os
    from datetime import datetime
    
    month_folder = datetime.now().strftime("%Y-%m")
    local_dir = os.path.join("client_data", client_id, financial_year, "invoices", month_folder)
    os.makedirs(local_dir, exist_ok=True)
    
    safe_vendor_name = "".join([c for c in node_input.vendor_name if c.isalpha() or c.isdigit() or c==' ']).rstrip()
    timestamp = datetime.now().strftime("%H%M%S")
    local_filename = os.path.join(local_dir, f"invoice_{safe_vendor_name}_{timestamp}.xlsx")
    
    with open(local_filename, "wb") as f:
        f.write(file_stream.getvalue())
    # ----------------------------
    
    # Create the binary part for ADK UI
    blob = types.Blob(mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", data=file_stream.read())
    part = types.Part(inline_data=blob)
    
    # Create a message describing the success
    message = f"Successfully processed invoice for {node_input.vendor_name}. Saved to physical folder: `{local_filename}` and attached below."
    text_part = types.Part.from_text(text=message)
    
    content = types.Content(role="model", parts=[text_part, part])
    
    # Emitting an event with content will render it in the UI, 
    # and ADK's SaveFilesAsArtifactsPlugin will automatically save the Excel file to the artifact store.
    yield Event(content=content, output=message)

invoice_processing_agent = Workflow(
    name="invoice_processing_agent",
    description="Processes raw invoice PDFs, categorizes line items, and generates the final Excel export.",
    edges=[
        ('START', invoice_extractor),
        (invoice_extractor, process_and_save_invoice)
    ]
)
