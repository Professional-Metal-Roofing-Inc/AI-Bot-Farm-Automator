import re
import xml.etree.ElementTree as ET

import uiautomator2 as u2

SERIAL = "R5CT604VB1N"

MESSAGE_MARKERS = (
    "double tap to see sent/receive date and time",
    "double tap and hold to react on message",
)


def clean_accessibility_text(value):
    value = value.strip()

    for marker in MESSAGE_MARKERS:
        position = value.lower().find(marker)
        if position != -1:
            value = value[:position]

    return re.sub(r",?\s*$", "", value).strip()


device = u2.connect(SERIAL)
root = ET.fromstring(device.dump_hierarchy())

print(f"App: {device.app_current().get('package')}")
print("Possible messages:\n")

found = 0

for node in root.iter():
    text = node.attrib.get("text", "").strip()
    description = node.attrib.get("content-desc", "").strip()

    for raw_value in (text, description):
        lowered = raw_value.lower()

        if any(marker in lowered for marker in MESSAGE_MARKERS):
            found += 1
            cleaned = clean_accessibility_text(raw_value)
            bounds = node.attrib.get("bounds", "unknown")

            print(f"[{found}] {cleaned}")
            print(f"    Bounds: {bounds}\n")
            break

if found == 0:
    print("No accessible conversation messages found.")

