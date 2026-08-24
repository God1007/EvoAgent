# Code Quality Skill

Detects newly introduced TODO/FIXME markers that describe unfinished production behavior.

Protocol: EvoAgent Review Skill v1. Input: parsed unified diff. Output: structured EvoAgent findings with precise new-file locations.
Each finding must use the exact v1 fields, quote bounded evidence from an added line, and carry its derived fingerprint.

