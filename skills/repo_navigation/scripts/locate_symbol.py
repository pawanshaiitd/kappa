# Symbol Locator Script
import sys

def suggest_search_query(symbol: str) -> str:
    parts = symbol.strip().split(".")
    return parts[-1]

if __name__ == "__main__":
    if len(sys.argv) > 1:
        print(suggest_search_query(sys.argv[1]))
