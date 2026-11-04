import pypdf
reader = pypdf.PdfReader("/root/nimbus/prd.pdf")
print("=== PAGE 4 ===")
print(reader.pages[3].extract_text())
