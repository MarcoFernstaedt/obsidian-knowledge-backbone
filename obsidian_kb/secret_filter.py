import re
from typing import List

SECRET_PATTERNS = [
    r'PRIVATE KEY', r'API[_-]?KEY', r'SECRET', r'PASSWORD',
    r'BEGIN [A-Z ]*PRIVATE KEY', r'aws_access_key_id', r'aws_secret_access_key',
]

def contains_secret(text: str, extra_patterns: List[str]=None) -> bool:
    patterns = SECRET_PATTERNS[:]
    if extra_patterns:
        patterns.extend(extra_patterns)
    for pat in patterns:
        if re.search(pat, text, re.IGNORECASE):
            return True
    return False
