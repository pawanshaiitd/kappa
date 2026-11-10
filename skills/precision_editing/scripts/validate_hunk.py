# Hunk Validator Helper
import sys

def check_hunk_lines(text: str) -> int:
    return len(text.strip().splitlines())

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(check_hunk_lines(sys.argv[1]))
