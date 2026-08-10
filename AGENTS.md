# Repository instructions

- Before changing charging semantics, sampling, voter parsing, or UI ordering, read `MAINTENANCE_NOTES.md` completely.
- Keep the Web and Android repositories behaviorally synchronized; do not copy their UI files wholesale because their data bridges differ.
- Never interpret `wireless_qc=100` as a final 100mA input limit. Final wireless input ICL comes from `wireless_buck_input effective`.
- Preserve the current-limit row grouping and the low-power sampling rules documented in `MAINTENANCE_NOTES.md`.
- Before publishing, run the repository-specific release checklist in `MAINTENANCE_NOTES.md`.
