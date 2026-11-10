# Multimodal Audio Input Specifications

## 1. Supported Audio Formats & Codecs
- **Container Formats**: WAV, MP3, FLAC, OGG, WebM.
- **Sample Rates**: 16,000 Hz (speech recognition standard) or 44,100 / 48,000 Hz.
- **Channels**: Mono preferred; stereo channels are downmixed to single channel prior to acoustic modeling.
- **Bit Depth**: 16-bit linear PCM.

## 2. Audio Token Budgeting & Chunking
- Audio segments are partitioned into bounded frames of 30 seconds maximum.
- Voice Activity Detection (VAD) automatically strips leading and trailing silence (>500ms) to conserve context tokens.
- Streaming audio chunks are processed incrementally with speculative beam search.
