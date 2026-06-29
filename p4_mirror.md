# P4Mirror - Incremental Perforce to GitHub Migration Framework

## Overview

### Purpose

P4Mirror is a lightweight migration framework that continuously synchronizes one Perforce depot path to one GitHub repository.

Unlike a one-time migration, P4Mirror incrementally mirrors new Perforce changelists into Git commits while preserving important metadata such as:

· Commit history

· Author

· Timestamp

· Commit message

· File additions

· File modifications

· File deletions

The framework is designed for organizations that are gradually transitioning from Perforce to GitHub while developers continue working in Perforce.

# Design Goals

· One Jenkins Freestyle Job = One GitHub Repository

· No Jenkins Pipeline required

· Uses Python for migration logic

· Uses Perforce CLI (p4.exe)

· Uses Git CLI

· Compatible with GitHub App authentication

· Supports repositories that are only a subset of a very large Perforce depot

· Incremental synchronization only

· Safe to rerun

· Resume after interruption

· Easy to duplicate for additional repositories

# High-Level Architecture

Developer

      │

      ▼

Perforce Submit

      │

      ▼

Jenkins Trigger

      │

      ▼

P4Mirror

      │

      ▼

GitHub

Each Perforce changelist becomes exactly one Git commit.

# Project Layout
```
P4Mirror/

    migrate.py

    config.py

    config/

        repository.json

        users.json

    core/

        p4\_client.py

        git\_client.py

        changelist.py

        state\_manager.py

        workspace.py

        migration.py

        logger.py

    state/

        last\_cl.txt

    logs/

    temp/

    README.md
```
# Repository Configuration

Each Jenkins job owns one configuration file.

Example:
```
repository.json

{

    "repository\_name": "ApplicationA",

    "p4\_port": "...",

    "p4\_user": "...",

    "p4\_client": "...",

    "depot\_path": "//Depot/ApplicationA/...",

    "workspace\_root": "D:/Jenkins/ApplicationA",

    "github\_url": "https://github.com/company/ApplicationA.git",

    "default\_branch": "main"

}
```
No Python code changes should be required for another repository.

# User Mapping

Perforce usernames usually don’t contain email addresses.

A mapping file will translate them.

Example:
```
users.json

{

    "john": {

        "name": "John Smith",

        "email": "john.smith@company.com"

    },

    "mary": {

        "name": "Mary Jones",

        "email": "mary.jones@company.com"

    }

}
```
# Migration Workflow

Every Jenkins execution performs the following:

    1. Read configuration.

    2. Verify Git repository exists.

    3. Verify Perforce workspace exists.

    4. Read last migrated changelist.

    5. Query Perforce for newer changelists.

    6. Sort changelists from oldest to newest.

    7. Process each changelist.

    8. Push all commits to GitHub.

    9. Save latest processed changelist.

# Changelist Processing

For every changelist:

    1. Retrieve metadata.

    2. Retrieve author.

    3. Retrieve timestamp.

    4. Retrieve description.

    5. Sync workspace to that changelist.

    6. Apply changes to Git.

    7. Create Git commit.

    8. Continue to next changelist.

Each Perforce changelist becomes one Git commit.

# Git Commit Metadata

Every commit should preserve:

Author Name
```
John Smith
```
Author Email
```
john.smith@company.com
```
Commit Date
```
2026-06-18T09:32:14
```
Commit Message
```
Fixed login timeout.

[P4 CL 58321]
```
# State Management

The framework stores only the latest successfully migrated changelist.

Example
```
state/

    state.json
```
Contents
```json
{
    "last_migrated_cl": 58321,
    "repository": "ApplicationA",
    "branch": "main",
    "last_run": "2026-06-25T10:15:30+00:00"
}
```
If the state file doesn't exist, is empty, or contains an invalid changelist
number, P4Mirror falls back to scanning the Git commit history for the last
Perforce changelist.  It looks for the ``[git-p4: ... change = N]`` marker
in commits that touched the configured sparse-checkout paths.  If a matching
commit is found, the state is reconstructed automatically.  If no matching
commit exists, the application stops with an error.

If Jenkins stops unexpectedly, the next execution resumes from the next
changelist.

# Logging

Every execution creates a timestamped log
```
logs/

    20260618\_101501.log
```
Logs should include:

    · Jenkins build number

    · Start time

    · End time

    · Number of changelists processed

    · Number of commits created

    · Push status

    · Errors

# Error Recovery

If migration fails during a changelist:

    · Stop immediately.

    · Do not update last\_cl.txt.

    · Leave Git repository unchanged beyond completed commits.

    · Next Jenkins run resumes automatically.

If last\_cl.txt file doesn’t exists or is empty or CL number is invalid:

    · Stop immediately.

    · Proper error message should be printed in the log file.

# Python Modules

Note: use uv package manager in creating the project

## migrate.py

Main entry point.

Responsibilities

    · Load configuration

    · Coordinate migration

    · Handle exceptions

    · Generate summary

## p4\_client.py

Wrapper around p4.exe.

Responsibilities

    · Execute p4 commands

    · Parse output

    · Return structured Python objects

## git\_client.py

Wrapper around Git CLI.

Responsibilities

    · git add

    · git rm

    · git commit

    · git push

    · Configure commit metadata

## changelist.py

Represents a Perforce changelist.

Contains:

    · ID

    · Author

    · Timestamp

    · Description

    · Changed files

## migration.py

Business logic.

Responsibilities

    · Determine new changelists

    · Process them sequentially

    · Coordinate Git and Perforce operations

## workspace.py

Workspace operations.

Responsibilities

    · Validate workspace

    · Initialize Git repository

    · Detect modified files

    · Clean temporary files

## state\_manager.py

Reads and writes migration state.

## logger.py

Central logging module.

# Jenkins Job

Freestyle Project

Build Trigger

    · Poll SCM

    · OR Perforce Trigger

Build Step

Execute Windows Batch Command
```
@echo off

python migrate.py
```
No Pipeline is required.