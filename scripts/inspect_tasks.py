"""Inspect sample tasks from tasks.jsonl."""

import json
from scripts.harness_tools import parse_unified_diff

def main():
    with open("tasks.jsonl", "r", encoding="utf-8") as f:
        for i in range(5):
            t = json.loads(f.readline())
            iid = t["instance_id"]
            repo = t["repo"]
            prob = t["problem_statement"][:120].replace("\n", " ").strip()
            print(f"=== Task {i+1}: {iid} ({repo}) ===")
            print(f"Problem: {prob}...")
            files = parse_unified_diff(t["patch"])
            for f_info in files:
                print(f"  Modified file: {f_info['new_file']}")

if __name__ == "__main__":
    main()
