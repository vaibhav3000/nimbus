import pypdf
reader = pypdf.PdfReader("/root/nimbus/prd.pdf")
print("=== PAGE 3 ===")
print(reader.pages[2].extract_text())
print("=== PAGE 5 ===")
print(reader.pages[4].extract_text())
print("=== PAGE 6 ===")
print(reader.pages[5].extract_text())
