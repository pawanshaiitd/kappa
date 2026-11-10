# Automated Speech Recognition (ASR) & Multimodal Audio Architecture

This directory specifies the multimodal audio and speech processing pipeline for the agent harness:
1. **Audio Ingestion & Preprocessing (`audio_spec.md`)**: Supported formats, sample rates, voice activity detection (VAD), and chunking.
2. **Transcription & Intent Pipeline (`transcription_pipeline.md`)**: Speech-to-text decoding, code symbol normalization, and direct intent mapping to tool calls.
3. **Structured Schema (`audio_schema.json`)**: Data interchange format for transcribed developer audio and voice directives.
