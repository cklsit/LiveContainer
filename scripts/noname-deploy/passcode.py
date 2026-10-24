"""HID keyboard mappings for numeric iPad passcodes."""

from __future__ import annotations

import re

HID_DIGIT_USAGES = {
    "1": 0x1E,
    "2": 0x1F,
    "3": 0x20,
    "4": 0x21,
    "5": 0x22,
    "6": 0x23,
    "7": 0x24,
    "8": 0x25,
    "9": 0x26,
    "0": 0x27,
}


def passcode_usages(passcode: str) -> list[int]:
    if not re.fullmatch(r"\d{4,12}", passcode):
        raise ValueError("The iPad passcode must contain 4 to 12 digits")
    return [HID_DIGIT_USAGES[digit] for digit in passcode]
