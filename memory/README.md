# Multi-Tier Agent Memory Architecture

This directory defines the memory management contracts for the agent harness, following Section 8 of `building_a_coding_harness_around_ai.md`:
1. **Working / Session Memory (`session_state.md`)**: In-flight reasoning state, active goal, tracked files, and problem description.
2. **Episodic Memory & Trajectory Distillation (`trajectory_cache.json`)**: Lessons distilled from previous task attempts, anti-pattern detections, and optimal recovery paths.
3. **Structured Facts & Memory Schema (`memory_schema.json`)**: Typed schema for project-specific constraints, dependency mappings, and API contracts.
