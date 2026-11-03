import pypdf
reader = pypdf.PdfReader("/root/nimbus/prd.pdf")
full_text = "\n".join(page.extract_text() for page in reader.pages)
for page_idx, page in enumerate(reader.pages):
    t = page.extract_text()
    if "Table 2" in t or "FR-1" in t or "Functional Requirements" in t or "NFR-1" in t:
        print(f"=== PAGE {page_idx+1} ===")
        print(t)
