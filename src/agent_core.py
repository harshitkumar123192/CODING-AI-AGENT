from __future__ import annotations

import json
import os
from dataclasses import dataclass

from openai import AzureOpenAI, OpenAI


@dataclass
class WorkItem:
    key: str
    title: str
    description: str


@dataclass
class AgentDecision:
    action: str
    reason: str


# ─────────────────────────────────────────────────────────────────────────────
# KEY LEARNING: All three providers below speak the same OpenAI chat.completions
# API format.  That means identical Python code works against all of them —
# only the base_url and api_key differ.  This is called the "OpenAI-compatible"
# interface and is now an industry standard.
#
# Provider selection order:
#   1. Databricks  (DATABRICKS_HOST + DATABRICKS_TOKEN)   ← you have this
#   2. Azure OpenAI (AZURE_OPENAI_ENDPOINT + key)
#   3. Public OpenAI (OPENAI_API_KEY)
# ─────────────────────────────────────────────────────────────────────────────

class LLMClient:
    def __init__(self) -> None:
        self.use_llm = os.getenv("USE_LLM", "false").lower() == "true"
        self.provider = "none"
        self.model = ""
        self.client = None

        if not self.use_llm:
            return

        # ── Option 1: Databricks Foundation Model APIs ──────────────────────
        db_host  = os.getenv("DATABRICKS_HOST", "").rstrip("/")
        db_token = os.getenv("DATABRICKS_TOKEN", "")
        if db_host and db_token:
            self.provider = "databricks"
            self.model = os.getenv(
                "DATABRICKS_MODEL_ENDPOINT",
                "databricks-claude-sonnet-5",   # default: most recent in your workspace
            )
            # Databricks serving endpoints are OpenAI-compatible.
            # We pass the workspace URL as base_url and the PAT as the api_key.
            self.client = OpenAI(
                api_key=db_token,
                base_url=f"{db_host}/serving-endpoints",
            )
            print(f"[LLM] Using Databricks: {self.model}")
            return

        # ── Option 2: Azure OpenAI ───────────────────────────────────────────
        az_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        az_key      = os.getenv("AZURE_OPENAI_API_KEY")
        if az_endpoint and az_key:
            self.provider = "azure_openai"
            self.model = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-4o-mini")
            self.client = AzureOpenAI(
                azure_endpoint=az_endpoint,
                api_key=az_key,
                api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
            )
            print(f"[LLM] Using Azure OpenAI: {self.model}")
            return

        # ── Option 3: Public OpenAI ──────────────────────────────────────────
        oai_key = os.getenv("OPENAI_API_KEY")
        if oai_key:
            self.provider = "openai"
            self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            self.client = OpenAI(api_key=oai_key)
            print(f"[LLM] Using OpenAI: {self.model}")
            return

        raise EnvironmentError(
            "USE_LLM=true but no credentials found.\n"
            "Set DATABRICKS_HOST + DATABRICKS_TOKEN  (recommended)\n"
            "  or AZURE_OPENAI_ENDPOINT + AZURE_OPENAI_API_KEY\n"
            "  or OPENAI_API_KEY"
        )

    def decide_next_action(
        self,
        work_item: WorkItem,
        completed_actions: list[str],
    ) -> AgentDecision:
        # ── LEARNING NOTE ────────────────────────────────────────────────────
        # LLMs are stateless: every call starts with zero memory.
        # If we only pass "current_step: 8" the model guesses what happened
        # in steps 1-7 and loops.  The fix is to pass the full history of
        # completed actions so the model always knows exactly where we are.
        # This is the #1 rule of agent design.
        # ─────────────────────────────────────────────────────────────────────

        # Safe fallback path lets you run and learn without API keys.
        if not self.use_llm or self.client is None:
            # Simulates a realistic one-round-of-review cycle:
            # PR is created → reviewer requests a change → rework → re-review → approve → merge → deploy → smoke test
            fallback = [
                AgentDecision("analyze_story", "Understand acceptance criteria."),
                AgentDecision("create_feature_branch", "Create feature branch from main."),
                AgentDecision("apply_code_change", "Implement the required change on the feature branch."),
                AgentDecision("run_tests", "Run pytest to validate the change before pushing."),
                AgentDecision("commit_and_push", "Commit changes and push feature branch to remote."),
                AgentDecision("create_pr", "Open PR from feature branch into main and request review."),
                AgentDecision("wait_for_review", "Pause until reviewer approves or requests changes."),
                AgentDecision("address_review_comments", "Reviewer left comments — fix the code and push again."),
                AgentDecision("run_tests", "Re-run tests after addressing review comments."),
                AgentDecision("commit_and_push", "Push updated changes to the same feature branch."),
                AgentDecision("wait_for_review", "Wait for reviewer to re-examine and approve."),
                AgentDecision("merge_pr", "PR approved — merge feature branch into main."),
                AgentDecision("deploy", "Find the ADO pipeline run triggered by the merge, wait for DEV stage approval and completion."),
                AgentDecision("smoke_test", "Verify the live app behaves correctly after deploy."),
                AgentDecision("done", "Work item complete."),
            ]
            return fallback[min(len(completed_actions), len(fallback) - 1)]

        prompt = {
            "story": {
                "key":         work_item.key,
                "title":       work_item.title,
                "description": work_item.description,
            },
            # Keep only the last 12 completed actions to stay within token limits.
            # Earlier history is summarised implicitly by what remains.
            "completed_actions": completed_actions[-12:],
            "total_steps_done": len(completed_actions),
            "valid_next_actions": [
                "analyze_story",
                "create_feature_branch",
                "apply_code_change",
                "run_tests",
                "commit_and_push",
                "create_pr",
                "wait_for_review",
                "address_review_comments",
                "merge_pr",
                "deploy",
                "smoke_test",
                "done",
            ],
            "instruction": (
                "You are a software delivery agent.  "
                "Look at completed_actions and pick the single best NEXT action "
                "from valid_next_actions.  "
                "Never repeat an action already in completed_actions unless "
                "address_review_comments or run_tests are genuinely needed again after a review round.  "
                "Return ONLY a JSON object with two keys: action and reason.  "
                "No markdown, no extra text."
            ),
        }

        # chat.completions is the universal format — same call works for
        # Databricks, Azure OpenAI, and public OpenAI.
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": json.dumps(prompt)}],
            max_tokens=200,
        )

        content = response.choices[0].message.content
        # Claude may return a list of content blocks instead of a plain string
        if isinstance(content, list):
            raw = "".join(block.get("text", "") if isinstance(block, dict) else str(block) for block in content).strip()
        else:
            raw = (content or "").strip()
        if not raw:
            # LLM returned empty content (token limit hit) — infer next action from history
            last = completed_actions[-1] if completed_actions else ""
            if "wait_for_review:changes_requested" in last:
                return AgentDecision("address_review_comments", "Reviewer requested changes.")
            if "address_review_comments" in last:
                return AgentDecision("run_tests", "Re-run tests after addressing review comments.")
            if "run_tests" in last and "commit_and_push" not in completed_actions[-2:]:
                return AgentDecision("commit_and_push", "Commit and push after tests passed.")
            if "commit_and_push" in last:
                return AgentDecision("wait_for_review", "Wait for reviewer response.")
            if "wait_for_review:approved" in last:
                return AgentDecision("merge_pr", "PR approved — merge.")
            return AgentDecision("done", "Fallback: workflow complete.")
        # Strip markdown code fences if the model wraps its JSON
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        parsed = json.loads(raw)
        return AgentDecision(action=parsed["action"], reason=parsed["reason"])
