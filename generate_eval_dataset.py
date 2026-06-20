import json
import random
import os
try:
    from reportlab.pdfgen import canvas
except ImportError:
    print("Please install reportlab: uv pip install reportlab")
    exit(1)

vendors = ["Acme Corp", "Global Tech Supplies", "Consulting LLC", "Cloud Hosting Inc", "Office Depot", "Legal Services LLP", "Cleaning Co", "Marketing Agency", "Logistics Partners", "Design Studio", "Acme Services", "Xero Services", "AWS", "Google Cloud", "Wework"]
descriptions = ["Software License", "Server Hosting", "Consulting Fee", "Office Supplies", "Legal Consultation", "Cleaning Services", "Ad Campaign", "Shipping", "Graphic Design", "Hardware Purchase", "Audit Management", "Subscription"]

os.makedirs("tests/eval_invoices", exist_ok=True)
dataset = []

print("Generating 20 diverse invoice PDFs...")
for i in range(20):
    vendor = random.choice(vendors)
    date = f"2026-{random.randint(1,12):02d}-{random.randint(1,28):02d}"
    
    # Randomly generate 1 to 4 line items
    num_items = random.randint(1, 4)
    items = []
    total = 0
    for _ in range(num_items):
        amt = round(random.uniform(50, 1500), 2)
        total += amt
        items.append((random.choice(descriptions), amt))
        
    filename = f"tests/eval_invoices/invoice_{i+1}.pdf"
    
    # Generate PDF with a realistic-ish layout
    c = canvas.Canvas(filename)
    c.setFont("Helvetica-Bold", 16)
    c.drawString(50, 800, "INVOICE")
    
    c.setFont("Helvetica", 12)
    c.drawString(50, 770, f"From: {vendor}")
    c.drawString(400, 770, f"Date: {date}")
    c.drawString(400, 750, f"Invoice #: INV-2026-{i+1:03d}")
    
    c.drawString(50, 700, "Bill To: Ledgr Client")
    
    # Draw line items
    y = 650
    c.setFont("Helvetica-Bold", 10)
    c.drawString(50, y, "Description")
    c.drawString(400, y, "Amount")
    y -= 20
    
    c.setFont("Helvetica", 10)
    for desc, amt in items:
        c.drawString(50, y, desc)
        c.drawString(400, y, f"${amt:.2f}")
        y -= 20
        
    y -= 20
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, y, "Total:")
    c.drawString(400, y, f"${total:.2f}")
    
    c.save()
    
    # Create the ADK Eval JSONL payload
    dataset.append({
        "turn_id": f"eval_inv_{i+1}",
        "turn_history": [],
        "request": "Please process this invoice and generate the excel export.",
        "files": [filename]
    })

with open("eval_dataset.jsonl", "w") as f:
    for item in dataset:
        f.write(json.dumps(item) + "\n")

print("Generated 20 invoices in tests/eval_invoices/ and created eval_dataset.jsonl!")
