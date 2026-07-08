# Dev Workflow Agent — Learning Project

A Python agent that drives your full software delivery workflow using an LLM as its brain.
Built step-by-step as a learning project, targeting a real Flask app on Azure App Service.

---

## The workflow the agent executes

This mirrors exactly what you do manually today:

```
1.  analyze_story          Read the Jira story, extract acceptance criteria
2.  create_feature_branch  Branch off main  →  feature/DYN-123
3.  apply_code_change      Implement the code change on the feature branch
4.  run_tests              Run pytest to validate before pushing
5.  commit_and_push        Commit + push the feature branch to remote
6.  create_pr              Open PR: feature/DYN-123 → main, request review
7.  wait_for_review        ← human-in-the-loop gate (you are the reviewer)
      ↳ CHANGES_REQUESTED  → address_review_comments → run_tests
                             → commit_and_push → wait_for_review (loop)
      ↳ APPROVED           → continue
8.  merge_pr               Merge the approved PR into main
9.  deploy                 Pipeline deploys main to Azure App Service
10. smoke_test             HTTP check against live App Service URL
11. done
```

---

## Key lessons learned while building this

### Lesson 1 — LLMs are stateless: you are the memory

The first time we connected a real LLM the agent looped endlessly on `smoke_test`.
The cause: we passed only `current_step: 8` — a number.
The LLM had no idea what happened in steps 1–7 so it guessed, and guessed wrong.

The fix: pass the full list of `completed_actions` to every LLM call.
The model can then read "analyze_story, create_feature_branch, apply_code_change..."
and reason correctly: "tests passed, PR was reviewed, now I should merge".

Rule: never pass a step counter to an LLM. Always pass the full history.

### Lesson 2 — Human-in-the-loop is a first-class workflow step

`wait_for_review` returns `ok=False` when the reviewer requests changes.
That signal causes the agent to rework rather than blindly proceeding.
In production this step polls the Azure DevOps PR API until you take an action.

### Lesson 3 — All major LLM providers speak the same API

Databricks Foundation Models, Azure OpenAI, and public OpenAI all implement
the OpenAI chat.completions interface. Identical Python code calls all three —
only `base_url` and `api_key` change. This project detects which provider
is configured and connects automatically.

---

## Stack

| Layer            | Technology                                      |
|------------------|-------------------------------------------------|
| LLM (live)       | Claude Sonnet 5 via Databricks Foundation APIs  |
| LLM (fallback)   | Hard-coded sequence, no API key needed          |
| Alt providers    | Azure OpenAI, public OpenAI (auto-detected)     |
| Target app       | Python Flask on Azure App Service               |
| Source control   | Azure DevOps Git                                |
| Work tracking    | Jira (finthrive.atlassian.net, project DYN)     |
| IDE              | VS Code + Databricks extension                  |

---

## Project structure

```
src/
  main.py         Agent loop — runs actions, tracks completed history, enforces post-review sequence
  agent_core.py   LLM client — provider detection, prompt, decision parsing
  tools.py        Tool implementations — Jira, ADO Git/PR/Pipeline all live; smoke_test still mock
.env              Your credentials (gitignored, never commit)
.env.example      Template showing all supported variables
requirements.txt  Python dependencies
```

---

## Getting started

### 1. Install dependencies

```powershell
pip install -r requirements.txt
```

### 2. Run in fallback mode (no credentials needed)

```powershell
# No .env needed — just run
python src/main.py
```

The agent completes all 11 steps using a hard-coded sequence.
Use this to understand the flow before connecting real systems.

### 3. Connect to your Databricks LLM

Copy `.env.example` to `.env` and fill in:

```
# LLM
DATABRICKS_HOST=https://your-workspace.azuredatabricks.net
DATABRICKS_TOKEN=your_personal_access_token
DATABRICKS_MODEL_ENDPOINT=databricks-claude-sonnet-5
USE_LLM=true

# Jira
JIRA_BASE_URL=https://your-org.atlassian.net
JIRA_USER_EMAIL=your@email.com
JIRA_API_TOKEN=your_jira_api_token
JIRA_ISSUE_KEY=DYN-123

# Azure DevOps
AZURE_DEVOPS_ORG_URL=https://dev.azure.com/your-org
AZURE_DEVOPS_PROJECT=your-project
AZURE_DEVOPS_REPO=your-repo
AZURE_DEVOPS_DEFAULT_BRANCH=main
AZURE_DEVOPS_APP_FOLDER=app
AZURE_DEVOPS_PIPELINE_NAME=your-pipeline-name

# Smoke test
APP_SERVICE_URL=https://your-app.azurewebsites.net
```

> **ADO authentication**: The agent uses `AzureCliCredential` — no PAT needed.
> Run `az login` once before starting the agent and it will obtain AAD bearer tokens automatically.

Getting the token: Databricks → top-right avatar → Settings → Developer
→ Access tokens → Generate new token.

Getting the endpoint name: Databricks → Serving → click the Claude Sonnet 5
row — the full name is shown at the top of the detail page.

```powershell
python src/main.py
# [LLM] Using Databricks: databricks-claude-sonnet-5
```

Now every action decision is made by Claude Sonnet 5. Watch how the reasoning
in each step references what has already been completed.

---

## Integration status

Most tools in `src/tools.py` make real API calls. The table below shows the current state:

| Tool                    | Status | Integration                                              |
|-------------------------|--------|----------------------------------------------------------|
| `get_work_item`         | ✅ Live | Jira REST API v3  GET /rest/api/3/issue/{key}            |
| `create_feature_branch` | ✅ Live | git clone + git checkout -b via subprocess               |
| `apply_code_change`     | ✅ Live | LLM reads story, generates patch, writes files           |
| `run_tests`             | ✅ Live | LLM generates test cases, subprocess runs pytest         |
| `commit_and_push`       | ✅ Live | git add / commit / push via subprocess                   |
| `create_pr`             | ✅ Live | Azure DevOps Pull Requests REST API v7.1                 |
| `wait_for_review`       | ✅ Live | Polls ADO PR votes until approved or changes requested   |
| `address_review_comments` | ✅ Live | Fetches PR comments, LLM applies fix to files          |
| `merge_pr`              | ✅ Live | Azure DevOps complete pull request API (squash merge)    |
| `deploy`                | ✅ Live | Finds ADO pipeline run, polls DEV stage gate approval    |
| `smoke_test`            | 🔲 Mock | Replace with `requests.get(APP_SERVICE_URL)` + assert   |

### Credentials checklist for .env

```
# LLM (pick one provider)
DATABRICKS_HOST=https://your-workspace.azuredatabricks.net
DATABRICKS_TOKEN=                      # Databricks → avatar → Settings → Developer → Access tokens
DATABRICKS_MODEL_ENDPOINT=databricks-claude-sonnet-5
USE_LLM=true

# Jira
JIRA_BASE_URL=https://finthrive.atlassian.net
JIRA_USER_EMAIL=your@email.com
JIRA_API_TOKEN=                        # Atlassian account → Security → API tokens
JIRA_ISSUE_KEY=DYN-123                 # The specific story the agent should work on

# Azure DevOps
AZURE_DEVOPS_ORG_URL=https://dev.azure.com/your-org
AZURE_DEVOPS_PROJECT=your-project-name
AZURE_DEVOPS_REPO=your-repo-name
AZURE_DEVOPS_DEFAULT_BRANCH=main       # optional, defaults to main
AZURE_DEVOPS_APP_FOLDER=app            # optional, folder within the repo to apply changes
AZURE_DEVOPS_PIPELINE_NAME=your-pipeline-name  # pipeline to monitor after merge

# ADO auth: uses AzureCliCredential — run 'az login' before starting the agent.
# No PAT required.

# Flask app smoke test
APP_SERVICE_URL=https://your-app.azurewebsites.net
```

---

## Long-term architecture (multi-agent)

Once the single agent is solid, split it into specialist agents:

```
Intake Agent    →  watches Jira board, picks up assigned stories
Planning Agent  →  breaks a story into code tasks and test tasks
Coding Agent    →  generates and applies code patches
Validation Agent→  runs tests, linting, static analysis
PR Agent        →  opens PR, writes summary, requests reviewers
Release Agent   →  monitors pipeline, triggers deploy, posts status
```

Start with this single-agent project first. Split only when the single agent
becomes too complex to reason about in one prompt.

---

## Security notes

- Never commit `.env` — it is listed in `.gitignore`.
- Use short-lived PATs with the minimum required scopes.
- Regenerate any token shared outside of secure channels immediately.
- For production: replace PATs with Azure Managed Identity or Key Vault references.
