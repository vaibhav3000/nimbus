try:
    import pypdf
except ImportError:
    import subprocess
    subprocess.run(["pip", "install", "pypdf"])
    import pypdf

reader = pypdf.PdfReader("/root/nimbus/prd.pdf")
full_text = "\n".join(page.extract_text() for page in reader.pages)
lines = [l.strip() for l in full_text.splitlines() if l.strip()]

for l in lines:
    if any(l.startswith(k) for k in ["FR-", "NFR-"]):
        print(l)
