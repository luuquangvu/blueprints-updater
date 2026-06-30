# AGENTS.md — PROJECT EXECUTION CONTRACT

> [!IMPORTANT]
> This document is the PRIMARY CONTRACT for AI Agent behavior.
> ALL rules are MANDATORY.
> Logic is BINARY: COMPLIANCE or FAILURE.

---

## 1. TOOLCHAIN

### Environment

- The execution environment MUST be POSIX-compatible.
- If the environment is not POSIX-compatible:
  -> STOP and report the environment mismatch.

### Python Execution

- ALL Python commands MUST use:

  `uv run <command>`

### Dependencies

- Install dependencies ONLY via:

  `uv sync`

### Code Quality Tools

Use the tools configured by the repository validation pipeline.

Current toolchain includes:

- `ruff`
- `ty`
- `pyright`
- `pytest`
- `interrogate`
- `prettier`

---

## 2. ANTI-HALLUCINATION RULES

### Evidence Requirements

- DO NOT invent:
  - file paths
  - symbols
  - commands
  - command results
  - log contents
  - validation outcomes
  - test results

- No assumption may be treated as fact until verified from:
  - repository contents
  - tool output
  - log files
  - validation markers

- Before modifying, deleting, retrying, or making a final conclusion, you MUST gather concrete evidence about the relevant:
  - files
  - symbols
  - call paths
  - configuration
  - logs
  - validation output

- For newly created code, evidence MUST justify:
  - target location
  - integration point
  - affected call path
  - configuration impact (if applicable)

### Evidence Citation

When citing evidence, include one or more of:

- file path
- function name
- class name
- symbol name
- log path
- validation marker

Use line numbers when available and stable.

Do NOT rely on line numbers as the sole evidence source.

### Evidence Quality

Evidence priority (highest to lowest):

1. Repository source code
2. Tool output
3. Validation output
4. Log output
5. Documentation
6. Assumptions

If sources conflict:

-> STOP and investigate the conflict before proceeding.

If evidence is ambiguous, stale, missing, or non-unique:

-> STOP and investigate or ask for clarification.

---

## 3. CHANGE SCOPE CONTROL

Match the scope of changes to the scope of the requested task.

For narrowly scoped tasks:

- Prefer the smallest change that satisfies the task.
- Avoid unrelated refactors.

For repository-wide standardization, migration, cleanup, or renaming tasks:

- Apply changes consistently across the intended scope.
- Do not artificially limit modifications solely to reduce change size.

General rules:

- Minimize unnecessary change surface area.
- Do NOT perform unrelated refactors.
- Do NOT modify architecture, dependencies, workflows, tooling, formatting rules, or tests unless:
  - required by the task, or
  - supported by concrete evidence.

- Changes MUST be limited to files and code regions necessary to satisfy the task.

If broader changes are required:

-> Explicitly report why they are necessary before making them.

---

## 4. READ BEFORE WRITE

Before modifying code, you MUST:

1. Read all directly related files.
2. Identify affected files and symbols.
3. Trace relevant call paths.
4. Verify assumptions using repository evidence.
5. Determine the minimal safe change for the requested scope.

If the relevant code path cannot be identified:

-> STOP and investigate or ask for clarification.

### File Modification Discipline

- Do NOT modify a file unless it has been read during the current task.

- Before modifying a file, you MUST:
  1. Read the relevant portion of the file.
  2. Identify the affected symbols, configuration, or code paths.
  3. Verify that the file is necessary for the requested change.

- Do NOT infer file contents from:
  - file names
  - repository structure
  - previous tasks
  - assumptions

- If a file has not been inspected during the current task:
  -> STOP and read the file before making changes.

- If multiple files may be affected:
  -> Read all candidate files before selecting the modification target.

---

## 5. VALIDATION

### Scope

Validation is REQUIRED for any modification task.

Read-only analysis tasks are exempt unless the user explicitly requires validation.

### Validation Timing

Do NOT run validation until all intended modifications for the current hypothesis are complete.

Avoid unnecessary validation cycles.

### Validation Entry Point

Run the repository validation entry point.

Current command:

`uv run tools/validate.py > scratch/validate.txt 2>&1`

### Success Condition

Validation is ONLY successful if this EXACT line appears:

`VALIDATION_SUCCESS`

### Deterministic Validation Tags

The validation pipeline emits deterministic markers:

- `VALIDATION_START`
- `STEP_START: <cmd>`
- `STEP_OK: <cmd>`
- `STEP_FAILED: <cmd>`
- `VALIDATION_SUCCESS`
- `VALIDATION_FAILED`
- `VALIDATION_ERROR`

### Failure Handling

- Maximum validation cycles: 5.
- NO BLIND RETRIES.

Each retry MUST include ALL of the following:

1. A newly identified cause from logs or evidence.
2. A concrete change to:
   - code,
   - configuration,
   - command, or
   - environment.
3. Fresh verification after re-running validation.

Re-running validation without a new hypothesis is FAILURE.

If the same root cause appears twice without a meaningful change:

-> STOP and report the root cause.

If validation still fails after 5 cycles:

-> STOP and report failure with root cause analysis.

---

## 6. COMPLETION CRITERIA

A task is COMPLETE ONLY IF:

1. Applicable validation passed.
2. Required evidence is present.
3. Relevant logs, including validation logs when applicable, were reviewed.
4. Evidence supports all material claims.
5. Rule conflicts were absent or explicitly reported.
6. Changes were limited to necessary files and code regions.

If ANY mandatory rule cannot be satisfied:

-> STOP and REPORT FAILURE.

For modification tasks, failure to satisfy mandatory rules MUST be explicitly reported.

Partial compliance is FAILURE.
