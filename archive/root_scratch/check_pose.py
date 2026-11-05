import subprocess
import re

out = subprocess.getoutput('IGN_PARTITION=custom_hospital_world ign topic -e -t /world/hospital/pose/info -n 1')
for i in range(1, 7):
    pat = rf'name:\s*"robot_{i}"\s*\n\s*id:\s*\d+\s*\n\s*position\s*\{{\s*x:\s*([^\s]+)\s*\n\s*y:\s*([^\s]+)'
    m = re.search(pat, out)
    if m:
        print(f"robot_{i}: x={float(m.group(1)):.2f}, y={float(m.group(2)):.2f}")
    else:
        # Check if position is empty (meaning 0,0)
        pat2 = rf'name:\s*"robot_{i}"'
        m2 = re.search(pat2, out)
        if m2:
            chunk = out[m2.start():m2.start()+250]
            print(f"robot_{i} chunk:\n{chunk}")
