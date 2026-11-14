# Build Mature Harness Script
import json
import os
from pathlib import Path

SUBMISSION_DIR = Path('/Users/pawankumar.s/Documents/gemma-4-developer-agent/sample_submission')

def write(rel_path: str, text: str):
    p = SUBMISSION_DIR / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.strip() + '
', encoding='utf-8')
    print(f'Wrote: {rel_path}')

print('Builder initialized')
