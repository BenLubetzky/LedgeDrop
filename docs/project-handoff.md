# LedgerDrop project handoff

**Status:** The project owner chose to close the project after Stage 9. There is
no planned Stage 10. Stage 2–8 product functionality and Stage 9 in-repository
deployment-readiness work are complete. This document is a handoff, not a claim
that every live-infrastructure acceptance rehearsal has passed.

## What works

LedgerDrop handles one English-language PDF invoice per upload (20 MB and 10
pages maximum). The backend stores the original PDF, extracts structured invoice
data, normalizes it, applies deterministic validation rules, and records an
`ACCEPTED` or `NEEDS_REVIEW` decision. Reviewers can approve or reject review
items, or append corrections that trigger normalization, validation, and a new
decision. The frontend provides upload, document listing, and review screens.

The Stage 9 code adds production images, Render Blueprints, an R2-compatible
storage backend, deployed-configuration checks, security controls, readiness and
metrics endpoints, logging, a smoke harness, and an [operator
runbook](stage-9-runbook.md). The [Stage 9 spec](stage-9-deployment-readiness.md)
records the deployment boundary and acceptance gate.

## Where to start if returning

1. Read [README.md](../README.md) for setup and [CLAUDE.md](../CLAUDE.md) for
   stage boundaries and invariants.
2. Follow one upload through `backend/app/api/`,
   `backend/app/services/processing/pipeline.py`, the extraction,
   normalization, validation, and decision service directories, and then the
   review/correction services. Read the corresponding `docs/stage-*.md` specs
   when a policy or lifecycle choice is unclear.
3. Run the local backend tests and frontend lint/build commands in the README
   before changing behavior. Use the deterministic fake extraction provider for
   offline development.
4. For a deployment, follow the runbook and record the Stage 9 acceptance
   evidence. A working free staging service alone does not satisfy the spec's
   production, rollback, restore, and alert checks.

## Boundaries and open choices

Authentication, organizations, reviewer identity verification, notifications,
downstream posting, non-PDF inputs, and autonomous processing were not built.
Reviewer names are unverified labels. The Stage 9 spec also marks several
deployment and operating values as provisional, including cost, residency,
availability, and alert defaults. Review those before relying on the system for
real business data or expanding its scope.

The repository does not contain a completed live operational-acceptance record.
Treat the deployment gate in the Stage 9 spec as outstanding until its checks
are recorded, even though the project is closed for feature development.

**Pre-handoff revision:** `938ad89894909a5511e1ad1405a953ab1ed50950` was
HEAD before this document was written. Record a new revision or release tag if
this documentation is committed as the final project state.
