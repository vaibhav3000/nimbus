import subprocess
import re

out = subprocess.getoutput('IGN_PARTITION=custom_hospital_world ign topic -e -t /world/hospital/pose/info -n 1')
print("Total length:", len(out))
for m in re.finditer(r'name:\s*"([^"]+)"', out):
    name = m.group(1)
    if "robot" in name:
        # find position after name
        sub = out[m.start():m.start()+400]
        pos = re.search(r'position\s*\{\s*x:\s*([^\s]+)\s*y:\s*([^\s]+)\s*z:\s*([^\s]+)', sub)
        if pos:
            print(f"{name}: x={float(pos.group(1)):.2f}, y={float(pos.group(2)):.2f}, z={float(pos.group(3)):.2f}")
