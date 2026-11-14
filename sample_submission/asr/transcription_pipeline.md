# Speech-to-Intent Transcription Pipeline

```
[Raw Developer Audio]
        |
        v
[Voice Activity Detection (VAD)] -> (Strip Silence)
        |
        v
[Acoustic Decoder (Whisper / Conformer / Gemma-Audio)]
        |
        v
[Code Symbol Post-Processing]
  - Normalizes spoken terms into code syntax:
    - "dot py" -> ".py"
    - "fast a p i" -> "FastAPI"
    - "snake_case", "camelCase" reconstruction
        |
        v
[Intent Classifier & Tool Routing]
  - Maps natural speech directly to tool calls:
    - "Read module openapi dot py lines 50 to 100" -> read_file(filepath="openapi.py", start_line=50, end_line=100)
    - "Run targeted test test underscore target" -> run_command(command="pytest tests/test_target.py")
```
