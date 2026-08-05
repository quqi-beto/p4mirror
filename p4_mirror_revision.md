# **P4Mirror - Incremental Perforce to GitHub Migration Framework (Revised)**

This revision incorporates the design decisions discussed during the review, including sparse checkout support, multiple path mappings, Jenkins as a trigger only, and strict preservation of Perforce history (1 Perforce changelist = 1 Git commit).

## **Key Design Principles**

· One Jenkins Freestyle Job = One GitHub repository.

· Jenkins is responsible only for SCM triggering/scheduling and launching P4Mirror.

· All migration logic is implemented inside P4Mirror.

· One relevant Perforce changelist becomes exactly one Git commit.

· Only configured Perforce depot paths are synchronized.

· Git Sparse Checkout mirrors the Perforce workspace.

· Migration is incremental, resumable, and safe to rerun.

## **Repository Configuration**

· Support multiple Perforce-to-Git path mappings.

· Example mapping: //RFB/AppA/... -> AppA, //RFB/AppC/... -> AppC.

· Sparse checkout configuration should match the mapped Git folders.
## Dynamic Depot Mappings

·  A path mapping with `"dynamic": true` watches a depot root as a whole
   (e.g. //REPOSITORY/...) and migrates every incremental change with no filter.

·  Sub-paths need not be known ahead of time (e.g. build artifacts committed
   under unique paths by an automated build server).

·  Files are committed WITHOUT being written to the local workspace
   (no-checkout staging: p4 print → git hash-object → git update-index) and are
   marked skip-worktree so git checkout/reset never materializes them.

·  Dynamic paths are excluded from sparse checkout; `sparse_checkout` must stay
   enabled for the no-materialization guarantee to hold.

·  Optional `baseline_cl` seeds the starting changelist so a depot can be
   adopted without backfilling its entire history (set to the current P4 change
   number at adoption time).
## **Migration Workflow**

· Load configuration and user mappings.

· Read per-path baselines from state file.

· Discover newer Perforce changelists — **each gitPath queries from its own baseline**.

· Union results, sort oldest to newest.

· For each changelist:
  - Fetch details to determine which gitPaths are affected.
  - Sync only the affected depot paths (per-path sync).
  - Stage changes, create one Git commit preserving author, timestamp and message.
  - Track per-path progress.

· Push commits to GitHub.

· Update per-path state independently.

## **Important Rules**

· Never sync directly to HEAD; doing so would collapse multiple Perforce changelists into one Git commit.

· Skipped changelists that modify only unmapped paths are ignored safely.

· Force-sync local workspace to remote origin before creating new commits.

## **State File**

· Use a JSON state file with per-gitPath tracking instead of a single `last_migrated_cl`.

· Each gitPath has its own `last_migrated_cl` so paths can progress independently.

· Legacy single-CL format is auto-converted on read.

· Cross-path changelists (modifying multiple gitPaths) sync all affected paths in one pass.

## **Long-Term Vision**

· P4Mirror is a reusable migration engine. New repositories require only configuration, a Perforce workspace, GitHub credentials, and a Jenkins job.