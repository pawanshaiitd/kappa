# Test Result Parser
import sys

def parse_exit_code(code: int) -> str:
    return "PASSED" if code == 0 else "FAILED"

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(parse_exit_code(int(sys.argv[1])))
