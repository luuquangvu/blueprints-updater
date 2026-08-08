# PROJECT EXECUTION CONTRACT

> [!IMPORTANT]
> This document defines the Primary Contract for AI Coding Agent behavior.
> All mandatory rules apply unless explicitly exempted by this contract.

---

## 1. TOOLCHAIN

### Environment

The execution environment must be POSIX-compatible.

If the environment is not POSIX-compatible: Stop and report the environment mismatch.

### Python Execution

Execute all Python commands using `uv run <command>`.

### Dependencies

Manage Python dependencies exclusively with `uv`.

### Code Quality

Use the code quality tools configured by the repository.

Repository toolchain:

- `ruff`
- `ty`
- `pyright`
- `pytest`
- `interrogate`
- `prettier`

### Coding Standards

- Prefer simple, explicit, type-safe, and maintainable implementations.
- Prefer precise types over `Any`.
- Use `typing.cast()` only as a last resort.
- Do not use `assert` in production code.
- Do not suppress or bypass static analysis.
- Fix root causes instead of suppressing warnings or errors.

---

## 2. ANTI-HALLUCINATION

### Evidence Requirements

Do not invent:

- file paths
- symbols
- commands
- command results
- log contents
- validation outcomes
- test results

Treat nothing as fact until it has been verified using repository evidence, including:

- repository contents
- tool output
- log files
- validation output

Before modifying code, deleting code, retrying validation, or making material conclusions, gather concrete evidence for the relevant:

- files
- symbols
- call paths
- configuration
- logs
- validation output

For newly created code, evidence must justify:

- target location
- integration point
- affected call path
- configuration impact (if applicable)

### Evidence Citation

Support non-obvious or material conclusions by citing repository evidence, including:

- file path
- function name
- class name
- symbol name
- log path
- validation marker

### Evidence Quality

Evidence priority (highest to lowest):

1. Repository source code
2. Tool output
3. Validation output
4. Log output
5. Repository documentation
6. Assumptions

If evidence conflicts: Stop and investigate.

If evidence is ambiguous, stale, missing, or non-unique: Stop and investigate or ask for clarification.

---

## 3. CHANGE SCOPE CONTROL

Match the scope of changes to the requested task.

For narrowly scoped tasks:

- Prefer the smallest change that fully satisfies the request.
- Avoid unrelated refactoring.

For repository-wide migrations, standardization, cleanup, or renaming:

- Apply changes consistently across the intended scope.
- Do not artificially limit modifications solely to reduce change size.

General rules:

- Minimize unnecessary changes.
- Do not perform unrelated refactoring.
- Do not modify architecture, dependencies, workflows, tooling, formatting rules, or tests unless:
  - required by the task; or
  - supported by repository evidence.
- Limit changes to the files and code regions necessary to complete the task.

If broader changes are required: Explain why before making them.

---

## 4. READ BEFORE WRITE

Before modifying code:

1. Read every file that is reasonably necessary to understand and safely implement the requested change.
2. Identify the affected files and symbols.
3. Trace the relevant call paths.
4. Verify assumptions using repository evidence.
5. Determine the minimal safe implementation.

If the relevant code path cannot be identified: Stop and investigate or ask for clarification.

### File Modification Discipline

Do not modify a file unless it has been read during the current task.

Before modifying a file:

1. Read the relevant portion of the file.
2. Identify the affected symbols, configuration, or call paths.
3. Verify that the file is required for the requested change.

Do not infer file contents from:

- file names
- repository structure
- previous tasks
- assumptions

If a file has not been inspected during the current task: Stop and read it before making changes.

If multiple files may be affected: Read all candidate files before selecting the modification target.

---

## 5. VALIDATION

### Scope

Validation is required for every modification task.

Read-only analysis tasks are exempt unless validation is explicitly requested.

### Timing

Run validation only after all intended modifications for the current hypothesis are complete.

Avoid unnecessary validation cycles.

### Entry Point

Run the repository validation entry point.

Default validation command:

`uv run tools/validate.py > scratch/validate.txt 2>&1`

### Success Condition

Validation succeeds only if the repository-defined success condition is met.

Current success marker: `VALIDATION_SUCCESS`

### Failure Handling

Maximum validation cycles: 5

Do not perform blind retries.

Each retry must include:

1. A newly identified root cause based on evidence.
2. A concrete change to:
   - code,
   - configuration,
   - command, or
   - environment.
3. Fresh validation after the change.

Do not re-run validation without a new hypothesis.

If the same root cause appears twice without a meaningful change: Stop and report the root cause.

If validation still fails after five cycles: Stop and report failure with root cause analysis.

---

## 6. COMPLETION CRITERIA

A task is complete only if:

1. The requested task has been satisfied.
2. Applicable validation has passed.
3. Required evidence has been collected.
4. Relevant validation output or logs have been reviewed.
5. Evidence supports all material conclusions.
6. Rule conflicts are absent or explicitly reported.
7. Changes are limited to the necessary files and code regions.

If any mandatory rule cannot be satisfied: Stop and report failure.

For modification tasks, failure to satisfy mandatory rules must be explicitly reported.

Partial compliance is failure.
