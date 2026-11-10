# Surgical Edit Examples

## Bad (Ambiguous):
```python
old_string = "    return None"
```
*Triggers `FileEditError: Multiple matches found`.*

## Good (Unique Context):
```python
old_string = """    def get_token(self, request: Request) -> Optional[str]:
        header = request.headers.get("Authorization")
        if not header:
            return None"""
```
*Matches exactly 1 occurrence.*
