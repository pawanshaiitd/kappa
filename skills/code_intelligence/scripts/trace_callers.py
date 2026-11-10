# AST Caller Tracer Helper
import sys

def format_node_query(node: str) -> str:
    return node.strip().replace("/", ".")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(format_node_query(sys.argv[1]))
