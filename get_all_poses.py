import subprocess
import re

for i in range(1, 7):
    cmd = f"IGN_PARTITION=custom_hospital_world ign topic -e -t /robot_{i}/odom -n 1"
    out = subprocess.getoutput(cmd)
    m = re.search(r'position\s*\{\s*x:\s*([^\s]+)\s*y:\s*([^\s]+)', out)
    if m:
        print(f"R{i} (robot_{i}) live pose: x={float(m.group(1)):.2f}, y={float(m.group(2)):.2f}")
    else:
        print(f"R{i} (robot_{i}) live pose: not received")
