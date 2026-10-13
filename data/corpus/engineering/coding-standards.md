---
doc_id: coding-standards
title: Coding Standards
min_role: employee
source: synthetic / Acme Financial (fictional)
---

# Coding Standards

## Code review

Every pull request requires approval from 2 reviewers, at least one of whom owns the affected code. Authors may not merge their own pull requests without review.

## Testing

New code must keep line coverage at or above 80%. Every bug fix must include a regression test.

## Python style

Python code is formatted and linted with ruff. Type hints are required on all public functions.
