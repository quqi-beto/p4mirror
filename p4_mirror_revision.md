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

## **Migration Workflow**

· Load configuration and user mappings.

· Read migration state (last migrated changelist).

· Discover all newer Perforce changelists.

· Filter changelists to those affecting configured paths.

· Sort oldest to newest.

· For each relevant changelist: sync workspace to that changelist, stage changes, create one Git commit preserving author, timestamp and message.

· Push commits to GitHub.

· Update migration state.

## **Important Rules**

· Never sync directly to HEAD; doing so would collapse multiple Perforce changelists into one Git commit.

· Skipped changelists that modify only unmapped paths are ignored safely.

· Run 'git fetch' and 'git pull --ff-only' before creating new commits.

## **Suggested State File**

· Use a JSON state file (last\_migrated\_cl, repository, branch, last\_run) instead of a plain text file for future extensibility.

## **Long-Term Vision**

· P4Mirror is a reusable migration engine. New repositories require only configuration, a Perforce workspace, GitHub credentials, and a Jenkins job.