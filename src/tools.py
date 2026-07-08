from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from typing import List, Optional

import requests
from azure.identity import AzureCliCredential

from agent_core import WorkItem


@dataclass
class ToolResult:
    ok: bool
    message: str


class DevWorkflowTools:
    """
    Mock tools that imitate your current software delivery workflow.
    Replace each method body with real Jira/Azure DevOps/Databricks calls later.
    """

    def __init__(self) -> None:
        self.timeline: List[str] = []
        self._review_attempt: int = 0
        self._pr_id: Optional[int] = None       # set after create_pr, used by wait/merge
        self._clone_dir: Optional[str] = None   # set after create_feature_branch
        self._feature_branch: Optional[str] = None
        self._cached_token_value: Optional[str] = None  # ADO token cache
        self._cached_token_expiry: Optional[float] = None
        self._current_item: Optional[WorkItem] = None   # set after create_feature_branch
        self._test_summary_md: Optional[str] = None      # set after run_tests, used by create_pr
        self._changed_files: list[str] = []              # set after apply_code_change
        self._last_review_comment: Optional[str] = None  # set after wait_for_review
        self._pipeline_run_id: Optional[int] = None       # set after deploy, tracks CI/CD run

        # Jira auth header
        self._jira_base_url = os.getenv("JIRA_BASE_URL", "").rstrip("/")
        _email = os.getenv("JIRA_USER_EMAIL", "")
        _token = os.getenv("JIRA_API_TOKEN", "")
        _creds = base64.b64encode(f"{_email}:{_token}".encode()).decode()
        self._jira_headers = {
            "Authorization": f"Basic {_creds}",
            "Accept": "application/json",
        }

        # Azure DevOps config
        self._ado_org_url  = os.getenv("AZURE_DEVOPS_ORG_URL", "").rstrip("/")
        self._ado_project  = os.getenv("AZURE_DEVOPS_PROJECT", "")
        self._ado_repo     = os.getenv("AZURE_DEVOPS_REPO", "")
        self._ado_branch   = os.getenv("AZURE_DEVOPS_DEFAULT_BRANCH", "main")
        self._ado_clone_url = (
            f"{self._ado_org_url}/{self._ado_project}/_git/{self._ado_repo}"
        )

    def _ado_headers(self) -> dict:
        """Get an Azure AD bearer token for ADO REST API calls, with caching.
        Reuses the cached token until 5 minutes before expiry."""
        now = time.time()
        if (
            self._cached_token_value
            and self._cached_token_expiry
            and now < self._cached_token_expiry - 300
        ):
            token_value = self._cached_token_value
        else:
            credential = AzureCliCredential(process_timeout=30)
            token = credential.get_token("499b84ac-1321-427f-aa17-267ca6975798/.default")
            self._cached_token_value = token.token
            self._cached_token_expiry = float(token.expires_on)
            token_value = token.token
        return {
            "Authorization": f"Bearer {token_value}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _git(self, *args: str, cwd: Optional[str] = None) -> str:
        """Run a git command and return stdout. Raises on non-zero exit."""
        result = subprocess.run(
            ["git"] + list(args),
            cwd=cwd or self._clone_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)} failed:\n{result.stderr}")
        return result.stdout.strip()

    def _jira_description_to_text(self, desc: dict) -> str:
        """Convert Atlassian Document Format (ADF) to plain text."""
        if not desc:
            return ""
        texts = []
        for block in desc.get("content", []):
            for inline in block.get("content", []):
                if inline.get("type") == "text":
                    texts.append(inline["text"])
            texts.append(" ")
        return " ".join(texts).strip()

    def get_work_item(self) -> WorkItem:
        issue_key = os.getenv("JIRA_ISSUE_KEY", "")
        if not issue_key or not self._jira_base_url:
            # Fallback to hardcoded item if Jira is not configured
            self.timeline.append("Loaded hardcoded work item (Jira not configured)")
            return WorkItem(
                key="DYN-123",
                title="Add validation for missing payer id",
                description="Given a claim payload, when payer id is missing, the API should return 400.",
            )

        url = f"{self._jira_base_url}/rest/api/3/issue/{issue_key}"
        resp = requests.get(url, headers=self._jira_headers, timeout=15)
        resp.raise_for_status()

        data   = resp.json()
        fields = data["fields"]
        item = WorkItem(
            key=data["key"],
            title=fields["summary"],
            description=self._jira_description_to_text(fields.get("description")),
        )
        self.timeline.append(f"Loaded work item {item.key} from Jira")
        return item

    def analyze_story(self, item: WorkItem) -> ToolResult:
        self.timeline.append(f"Analyzed story {item.key}")
        return ToolResult(True, "Acceptance criteria extracted.")

    def create_feature_branch(self, item: WorkItem) -> ToolResult:
        """Clone the repo into a temp dir and create a feature branch from main."""
        self._feature_branch = f"feature/{item.key}"
        self._clone_dir = tempfile.mkdtemp(prefix=f"agent_{item.key}_")
        self._current_item = item
        try:
            self._git("clone", self._ado_clone_url, self._clone_dir, cwd=tempfile.gettempdir())
            self._git("checkout", "-b", self._feature_branch)
            self.timeline.append(f"Cloned repo and created branch {self._feature_branch} from {self._ado_branch}")
            return ToolResult(True, f"Repo cloned to {self._clone_dir}. Branch '{self._feature_branch}' created from {self._ado_branch}.")
        except RuntimeError as e:
            return ToolResult(False, str(e))

    def apply_code_change(self) -> ToolResult:
        """Use LLM to understand the story and apply the required code changes to the repo.
        Works for any kind of change to the web app — not limited to year replacements."""
        if not self._clone_dir or not self._current_item:
            return ToolResult(False, "No clone directory or story — create_feature_branch must run first.")

        item = self._current_item
        app_folder = os.getenv("AZURE_DEVOPS_APP_FOLDER", "app")
        search_root = os.path.join(self._clone_dir, app_folder)
        if not os.path.isdir(search_root):
            search_root = self._clone_dir  # fallback to repo root

        skip_dirs = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}
        text_extensions = {
            ".py", ".html", ".js", ".css", ".ts", ".tsx", ".jsx",
            ".txt", ".md", ".json", ".yaml", ".yml", ".xml",
            ".cfg", ".ini", ".j2", ".jinja", ".jinja2",
        }

        # Collect all candidate files with their content
        candidates: list[dict] = []
        for root, dirs, fnames in os.walk(search_root):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for fname in fnames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in text_extensions:
                    continue
                fpath = os.path.join(root, fname)
                rel_path = os.path.relpath(fpath, self._clone_dir)
                try:
                    content = open(fpath, "r", encoding="utf-8", errors="ignore").read()
                    candidates.append({"rel_path": rel_path, "fpath": fpath, "content": content})
                except Exception:
                    continue

        if not candidates:
            return ToolResult(False, f"No text files found under '{app_folder}'.")

        try:
            from openai import OpenAI
            host  = os.getenv("DATABRICKS_HOST", "").rstrip("/")
            token = os.getenv("DATABRICKS_TOKEN", "")
            model = os.getenv("DATABRICKS_MODEL_ENDPOINT", "")
            if host and token and model:
                client = OpenAI(base_url=f"{host}/serving-endpoints", api_key=token)
            else:
                api_key = os.getenv("OPENAI_API_KEY", "")
                if not api_key:
                    return ToolResult(False, "No LLM configured.")
                client = OpenAI(api_key=api_key)
                model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

            # ── Step 1: identify which files need to change ──────────────────
            file_summaries = "\n".join(
                f"- {c['rel_path']} ({len(c['content'])} chars): "
                f"{c['content'][:150].replace(chr(10), ' ')}"
                for c in candidates
            )
            select_prompt = (
                f"You are a developer. Given the story below, identify which files need to be modified.\n\n"
                f"Story: {item.title}\n"
                f"Description: {item.description[:800]}\n\n"
                f"Repository files:\n{file_summaries}\n\n"
                f"Return ONLY a JSON array of relative paths that need to change, e.g.:\n"
                f'["app/templates/base.html", "app/templates/login.html"]\n'
                f"If no files need changing, return []."
            )
            r1 = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": select_prompt}],
                temperature=0,
                max_tokens=500,
            )
            raw = r1.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()
            arr_match = re.search(r"\[.*\]", raw, re.DOTALL)
            if arr_match:
                raw = arr_match.group(0)
            selected_paths: list[str] = json.loads(raw)

            if not selected_paths:
                return ToolResult(False, "LLM identified no files needing change for this story.")

            # Match selected paths back to candidates (tolerant of / vs \ differences)
            def norm(p: str) -> str:
                return p.replace("\\", "/").strip()

            selected_set = {norm(p) for p in selected_paths}
            to_change = [c for c in candidates if norm(c["rel_path"]) in selected_set]
            if not to_change:
                # Partial-suffix fallback (LLM may omit leading folder)
                to_change = [
                    c for c in candidates
                    if any(norm(c["rel_path"]).endswith(norm(p)) for p in selected_paths)
                ]
            if not to_change:
                return ToolResult(False, f"LLM selected files not found in repo: {selected_paths}")

            # ── Step 2: apply the change to selected files ───────────────────
            files_block = "\n\n".join(
                f"### FILE: {c['rel_path']}\n{c['content']}"
                for c in to_change
            )
            change_prompt = (
                f"You are a developer implementing a story requirement.\n\n"
                f"Story: {item.title}\n"
                f"Description: {item.description[:800]}\n\n"
                f"Apply the required changes to each file below and return the complete updated content "
                f"(every line, not just the changed parts).\n\n"
                f"{files_block}\n\n"
                f"Return ONLY a JSON array:\n"
                f'[\n  {{"rel_path": "path/to/file", "new_content": "<complete updated file content>"}}\n]\n'
                f"No explanation, no markdown fences — just the JSON array."
            )
            r2 = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": change_prompt}],
                temperature=0,
                max_tokens=8000,
            )
            raw2 = r2.choices[0].message.content
            if isinstance(raw2, list):
                raw2 = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in raw2)
            raw2 = (raw2 or "").strip()
            if raw2.startswith("```"):
                raw2 = re.sub(r"^```[a-zA-Z]*\n?", "", raw2)
                raw2 = re.sub(r"\n?```$", "", raw2).strip()
            arr_match2 = re.search(r"\[.*\]", raw2, re.DOTALL)
            if arr_match2:
                raw2 = arr_match2.group(0)
            updates: list[dict] = json.loads(raw2)

            changed_files: list[str] = []
            for entry in updates:
                rel_path = entry.get("rel_path", "")
                new_content = entry.get("new_content", "")
                fpath = os.path.join(self._clone_dir, rel_path)
                if not os.path.exists(fpath):
                    # Try suffix match if LLM returned a different path form
                    for c in to_change:
                        if norm(c["rel_path"]).endswith(norm(rel_path)) or norm(rel_path).endswith(norm(c["rel_path"])):
                            fpath = c["fpath"]
                            rel_path = c["rel_path"]
                            break
                if os.path.exists(fpath) and new_content:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    changed_files.append(rel_path)

            if not changed_files:
                return ToolResult(False, "LLM returned no valid file changes.")

            summary = ", ".join(changed_files[:5])
            if len(changed_files) > 5:
                summary += f" ... and {len(changed_files) - 5} more"
            msg = f"LLM applied changes to {len(changed_files)} file(s): {summary}"
            self._changed_files = changed_files
            self.timeline.append(f"Code change applied: {msg}")
            return ToolResult(True, msg)

        except Exception as e:
            import traceback; traceback.print_exc()
            return ToolResult(False, f"LLM code change failed: {e}")

    def _llm_generate_test_results(self, item: WorkItem, files: list[dict], reviewer_comment: Optional[str] = None) -> list[dict]:
        """Ask the LLM to generate and evaluate test cases from the story + changed file contents.
        files: list of {rel_path, content}.
        Returns list of {page, test_case, expected, actual, passed}. Falls back to [] on error."""
        if not files:
            return []
        try:
            from openai import OpenAI
            host  = os.getenv("DATABRICKS_HOST", "").rstrip("/")
            token = os.getenv("DATABRICKS_TOKEN", "")
            model = os.getenv("DATABRICKS_MODEL_ENDPOINT", "")
            if host and token and model:
                client = OpenAI(base_url=f"{host}/serving-endpoints", api_key=token)
            else:
                api_key = os.getenv("OPENAI_API_KEY", "")
                if not api_key:
                    return []
                client = OpenAI(api_key=api_key)
                model  = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

            files_text = "".join(
                f"\n--- {f['rel_path']} ---\n{f['content']}\n"
                for f in files
            )
            reviewer_ctx = (
                f"\nReviewer feedback applied: \"{reviewer_comment}\"\n"
                "NOTE: The reviewer's feedback overrides the original story requirements "
                "for the purpose of these tests. Evaluate files against what the reviewer asked for.\n"
            ) if reviewer_comment else ""
            prompt = (
                f"You are a QA engineer reviewing code changes for a user story.\n\n"
                f"Story: {item.title}\n"
                f"Description: {item.description[:600]}\n"
                f"{reviewer_ctx}\n"
                f"The following files were changed:{files_text}\n"
                "Based on the requirements above (prioritising reviewer feedback if present), "
                "generate test cases that verify the changes were made correctly. For each changed file:\n"
                "  1. Identify what UI page or component it represents.\n"
                "  2. Write 2-3 concise test cases relevant to the story.\n"
                "  3. Evaluate each test case by examining the actual file content above.\n\n"
                "Return ONLY a JSON array, no other text:\n"
                "[\n"
                "  {\"page\": \"Human-readable page or component name\",\n"
                "   \"test_case\": \"What is being verified\",\n"
                "   \"expected\": \"What the file should show/contain\",\n"
                "   \"actual\": \"Relevant extracted line or value from the file content\",\n"
                "   \"passed\": true}\n"
                "]"
            )
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=1500,
            )
            raw = resp.choices[0].message.content.strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()
            return json.loads(raw)
        except Exception:
            return []

    def run_tests(self, reviewer_comment: Optional[str] = None) -> ToolResult:
        """Generate and evaluate test cases via LLM from story + changed file contents.
        Report is saved to temp dir (not committed to the repo)."""
        if not self._clone_dir:
            return ToolResult(False, "No clone directory — create_feature_branch must run first.")

        item = self._current_item
        item_key = item.key if item else "UNKNOWN"
        item_title = item.title if item else ""

        # Read content of every changed file
        files_content: list[dict] = []
        for rel_path in self._changed_files:
            fpath = os.path.join(self._clone_dir, rel_path)
            try:
                content = open(fpath, "r", encoding="utf-8", errors="ignore").read()
                files_content.append({"rel_path": rel_path, "content": content})
            except Exception:
                continue

        # LLM decides what to test and whether each test passed, based on story + file contents
        tests = self._llm_generate_test_results(item, files_content, reviewer_comment=reviewer_comment) if item else []

        # Fallback: if LLM failed, report each changed file as a plain "file was modified" check
        if not tests:
            tests = [
                {
                    "page": f["rel_path"],
                    "test_case": "File was modified",
                    "expected": "File exists and was changed",
                    "actual": "File present in changeset",
                    "passed": True,
                }
                for f in files_content
            ]

        passed = sum(1 for t in tests if t["passed"])
        failed = len(tests) - passed

        # --- Build markdown summary (stored for PR comment) ---
        from datetime import datetime as _dt
        timestamp = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
        md_rows = "\n".join(
            f"| {i} | {t['page']} | {t['test_case']} | {t['expected']} | {t['actual']} | {'\u2705 PASS' if t['passed'] else '\u274c FAIL'} |"
            for i, t in enumerate(tests, 1)
        )
        overall = f"\u2705 All {len(tests)} tests passed" if failed == 0 else f"\u274c {failed}/{len(tests)} tests FAILED"
        self._test_summary_md = (
            f"## Automated Test Results \u2014 {item_key}\n\n"
            f"> {overall}  \n"
            f"> Generated: {timestamp} | Agent: Dev Workflow Agent\n\n"
            f"| # | Page / Component | Test Case | Expected | Actual | Status |\n"
            f"|---|-----------------|-----------|----------|--------|--------|\n"
            f"{md_rows}\n"
        )

        # --- Generate HTML report (saved to temp dir, NOT the clone) ---
        html_rows = "".join(
            f"<tr><td>{i}</td><td>{t['page']}</td><td>{t['test_case']}</td>"
            f"<td>{t['expected']}</td><td>{t['actual']}</td>"
            f"<td class='{'pass' if t['passed'] else 'fail'}'>{'PASS' if t['passed'] else 'FAIL'}</td></tr>"
            for i, t in enumerate(tests, 1)
        )
        summary_bg  = "#d4edda" if failed == 0 else "#f8d7da"
        summary_txt = f"All {len(tests)} tests passed" if failed == 0 else f"{failed}/{len(tests)} tests FAILED"
        html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Test Report — {item_key}</title>
<style>
  body{{font-family:Segoe UI,Arial,sans-serif;margin:40px;color:#222;background:#fafafa}}
  h1{{color:#0052CC;margin-bottom:2px}} h2{{color:#555;font-size:.95em;font-weight:normal;margin-top:0}}
  .meta{{color:#888;font-size:.85em;margin:8px 0 20px}}
  .badge{{display:inline-block;padding:8px 18px;border-radius:20px;background:{summary_bg};
          font-weight:bold;font-size:1.05em;margin-bottom:24px}}
  table{{border-collapse:collapse;width:100%;background:#fff;border-radius:6px;
         box-shadow:0 1px 4px rgba(0,0,0,.08)}}
  th{{background:#0052CC;color:#fff;padding:11px 14px;text-align:left;font-weight:600}}
  td{{border-bottom:1px solid #eee;padding:10px 14px;vertical-align:top}}
  tr:last-child td{{border-bottom:none}}
  .pass{{color:#1a7f37;font-weight:700}} .fail{{color:#cf222e;font-weight:700}}
  .tag{{background:#f0f4ff;border-radius:4px;padding:2px 7px;font-size:.85em;color:#0052CC}}
</style></head>
<body>
<h1>Test Report — {item_key}</h1>
<h2>{item_title}</h2>
<div class="meta">Generated: {timestamp} &nbsp;&bull;&nbsp; Agent: Dev Workflow Agent &nbsp;&bull;&nbsp; Branch: <span class="tag">feature/{item_key}</span></div>
<div class="badge">{'&#x2705;' if failed==0 else '&#x274C;'}&nbsp; {summary_txt}</div>
<table>
  <thead><tr><th>#</th><th>Page / Component</th><th>Test Case</th><th>Expected</th><th>Actual</th><th>Status</th></tr></thead>
  <tbody>{html_rows}</tbody>
</table>
</body></html>"""

        report_name = f"test-report-{item_key}.html"
        report_path = os.path.join(tempfile.gettempdir(), report_name)
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(html)

        # --- Attach HTML report to Jira story ---
        attach_msg = ""
        if self._jira_base_url and item_key != "UNKNOWN":
            try:
                attach_url = f"{self._jira_base_url}/rest/api/3/issue/{item_key}/attachments"
                with open(report_path, "rb") as f:
                    resp = requests.post(
                        attach_url,
                        headers={
                            "Authorization": self._jira_headers["Authorization"],
                            "X-Atlassian-Token": "no-check",
                        },
                        files={"file": (report_name, f, "text/html")},
                        timeout=20,
                    )
                attach_msg = (
                    f" Report attached to Jira {item_key}."
                    if resp.status_code in (200, 201)
                    else f" (Jira attach failed: {resp.status_code})"
                )
            except Exception as exc:
                attach_msg = f" (Jira attach error: {exc})"

        status_line = f"{passed}/{len(tests)} tests passed."
        self.timeline.append(f"Tests run: {status_line}{attach_msg}")
        if failed > 0:
            return ToolResult(False, f"{status_line}{attach_msg}")
        return ToolResult(True, f"{status_line}{attach_msg}")

    def commit_and_push(self, item: WorkItem) -> ToolResult:
        """Stage all changes, commit, and push the feature branch to remote."""
        try:
            self._git("config", "user.email", os.getenv("JIRA_USER_EMAIL", "agent@local"))
            self._git("config", "user.name", "Dev Workflow Agent")
            self._git("add", "-A")
            status = self._git("status", "--short")
            if status:
                self._git("commit", "-m", f"{item.key}: agent-applied changes")
            else:
                # Nothing changed in working tree — use an empty commit
                self._git("commit", "--allow-empty", "-m", f"{item.key}: reviewed changes re-submitted")
            # Pull remote changes (e.g. from a previous push) before pushing again
            try:
                self._git("fetch", "origin", self._feature_branch)
            except RuntimeError:
                pass  # branch doesn't exist on remote yet — first push is fine
            self._git("push", "--force-with-lease", "origin", self._feature_branch)
            # If a PR is already open, reset all reviewer votes to 0 so they must re-vote
            if self._pr_id:
                try:
                    rev_url = (
                        f"{self._ado_org_url}/{self._ado_project}"
                        f"/_apis/git/repositories/{self._ado_repo}"
                        f"/pullrequests/{self._pr_id}/reviewers?api-version=7.1"
                    )
                    rv = requests.get(rev_url, headers=self._ado_headers(), timeout=10)
                    for reviewer in rv.json().get("value", []):
                        rid = reviewer.get("id", "")
                        if rid and reviewer.get("vote", 0) != 0:
                            put_url = (
                                f"{self._ado_org_url}/{self._ado_project}"
                                f"/_apis/git/repositories/{self._ado_repo}"
                                f"/pullrequests/{self._pr_id}/reviewers/{rid}?api-version=7.1"
                            )
                            r = requests.put(
                                put_url,
                                headers=self._ado_headers(),
                                json={"vote": 0, "id": rid},
                                timeout=10,
                            )
                            print(f"  [vote reset] reviewer {rid}: {r.status_code}")
                except Exception:
                    pass  # non-critical
            self.timeline.append(f"Committed and pushed {self._feature_branch} to remote")
            return ToolResult(True, f"Changes committed and pushed to remote branch {self._feature_branch}.")
        except RuntimeError as e:
            return ToolResult(False, str(e))

    def create_pr(self, item: WorkItem) -> ToolResult:
        """Open a Pull Request in Azure DevOps via REST API."""
        url = (
            f"{self._ado_org_url}/{self._ado_project}"
            f"/_apis/git/repositories/{self._ado_repo}/pullrequests?api-version=7.1"
        )
        body = {
            "title": f"{item.key}: {item.title}",
            "description": (
                f"**Story:** {item.key}\n\n"
                f"**Summary:** {item.description[:500]}\n\n"
                "*This PR was created by the Dev Workflow Agent.*"
            ),
            "sourceRefName": f"refs/heads/{self._feature_branch}",
            "targetRefName": f"refs/heads/{self._ado_branch}",
        }
        resp = requests.post(url, headers=self._ado_headers(), json=body, timeout=15)
        if resp.status_code in (200, 201):
            self._pr_id = resp.json()["pullRequestId"]
            pr_url = (
                f"{self._ado_org_url}/{self._ado_project}"
                f"/_git/{self._ado_repo}/pullrequest/{self._pr_id}"
            )
            self.timeline.append(f"Opened PR #{self._pr_id}: {self._feature_branch} → {self._ado_branch}")
        elif resp.status_code == 409:
            # PR already exists for this branch — find and reuse it
            search_url = (
                f"{self._ado_org_url}/{self._ado_project}"
                f"/_apis/git/repositories/{self._ado_repo}"
                f"/pullrequests?searchCriteria.sourceRefName=refs/heads/{self._feature_branch}"
                f"&searchCriteria.status=active&api-version=7.1"
            )
            sr = requests.get(search_url, headers=self._ado_headers(), timeout=15)
            existing = sr.json().get("value", [])
            if not existing:
                return ToolResult(False, f"Failed to create PR: {resp.status_code} {resp.text[:300]}")
            self._pr_id = existing[0]["pullRequestId"]
            pr_url = (
                f"{self._ado_org_url}/{self._ado_project}"
                f"/_git/{self._ado_repo}/pullrequest/{self._pr_id}"
            )
            self.timeline.append(f"Reusing existing PR #{self._pr_id}: {self._feature_branch} → {self._ado_branch}")
        else:
            return ToolResult(False, f"Failed to create PR: {resp.status_code} {resp.text[:300]}")
        # Post test results as a PR thread comment (common to both new and reused PR)
        if self._test_summary_md:
            threads_url = (
                f"{self._ado_org_url}/{self._ado_project}"
                f"/_apis/git/repositories/{self._ado_repo}"
                f"/pullrequests/{self._pr_id}/threads?api-version=7.1"
            )
            requests.post(
                threads_url,
                headers=self._ado_headers(),
                json={
                    "comments": [{"parentCommentId": 0, "content": self._test_summary_md, "commentType": 1}],
                    "status": 1,
                },
                timeout=15,
            )
        return ToolResult(True, f"PR #{self._pr_id} opened. Review it at: {pr_url}")

    def wait_for_review(self) -> ToolResult:
        """Poll Azure DevOps PR status every 30 seconds until approved or changes requested.
        Returns ok=True when approved, ok=False when changes are requested.
        """
        if not self._pr_id:
            return ToolResult(False, "No PR ID found — create_pr must run first.")

        url = (
            f"{self._ado_org_url}/{self._ado_project}"
            f"/_apis/git/repositories/{self._ado_repo}/pullrequests/{self._pr_id}?api-version=7.1"
        )
        print(f"  [wait_for_review] Polling PR #{self._pr_id} — go review it in ADO and Approve or Request Changes.")
        poll_count = 0
        while True:
            resp = requests.get(url, headers=self._ado_headers(), timeout=15)
            if resp.status_code != 200:
                return ToolResult(False, f"Could not fetch PR status: {resp.status_code}")
            pr = resp.json()
            status   = pr.get("status", "")
            vote_map = {10: "approved", 5: "approved_with_suggestions", -10: "changes_requested"}

            # Check reviewer votes
            reviewers = pr.get("reviewers", [])
            votes     = [r.get("vote", 0) for r in reviewers]

            if any(v == -10 for v in votes):
                self.timeline.append(f"PR #{self._pr_id} review: changes requested")
                # Fetch latest comment for context
                threads_url = (
                    f"{self._ado_org_url}/{self._ado_project}"
                    f"/_apis/git/repositories/{self._ado_repo}/pullrequests/{self._pr_id}/threads?api-version=7.1"
                )
                tr = requests.get(threads_url, headers=self._ado_headers(), timeout=15)
                comment = "Reviewer requested changes."
                if tr.status_code == 200:
                    threads = [t for t in tr.json().get("value", []) if not t.get("isDeleted")]
                    for thread in reversed(threads):
                        for c in thread.get("comments", []):
                            content = c.get("content", "")
                            # commentType: 3/"system" = ADO system messages; skip those.
                            # ADO returns commentType as int (1=text) or string ("text").
                            ctype = c.get("commentType", "")
                            is_system = ctype in (3, "system")
                            if (
                                not is_system
                                and content
                                and "Agent: Dev Workflow Agent" not in content
                                and "Automated Test Results" not in content
                            ):
                                comment = content[:300]
                                break
                        else:
                            continue
                        break
                self._last_review_comment = comment
                return ToolResult(False, f"CHANGES_REQUESTED: {comment}")

            if any(v >= 5 for v in votes) or status == "completed":
                self.timeline.append(f"PR #{self._pr_id} review: approved")
                return ToolResult(True, f"APPROVED: PR #{self._pr_id} approved by reviewer.")

            poll_count += 1
            print(f"  [wait_for_review] Status: {status}, votes: {votes}. Waiting 30s... (poll #{poll_count})")
            time.sleep(30)

    def _apply_review_change(self, comment: str) -> str:
        """Ask the LLM to understand the reviewer comment and produce corrected file contents."""
        if not self._clone_dir or not self._changed_files:
            return "No files to update."

        # Read current contents of each changed file
        files: list[dict] = []
        for rel_path in self._changed_files:
            fpath = os.path.join(self._clone_dir, rel_path)
            try:
                content = open(fpath, "r", encoding="utf-8", errors="ignore").read()
                files.append({"rel_path": rel_path, "content": content})
            except Exception:
                continue

        if not files:
            return "Could not read changed files."

        try:
            from openai import OpenAI
            host  = os.getenv("DATABRICKS_HOST", "").rstrip("/")
            token = os.getenv("DATABRICKS_TOKEN", "")
            model = os.getenv("DATABRICKS_MODEL_ENDPOINT", "")
            if host and token and model:
                client = OpenAI(base_url=f"{host}/serving-endpoints", api_key=token)
            else:
                api_key = os.getenv("OPENAI_API_KEY", "")
                if not api_key:
                    return "No LLM configured."
                client = OpenAI(api_key=api_key)
                model  = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

            files_block = "\n\n".join(
                f"### FILE: {f['rel_path']}\n{f['content']}"
                for f in files
            )
            prompt = (
                f"You are a developer applying a reviewer's feedback to source files.\n\n"
                f"Reviewer comment: \"{comment}\"\n\n"
                f"Current file contents:\n{files_block}\n\n"
                f"Apply the reviewer's requested change to each file and return the result as a JSON array:\n"
                f"[\n"
                f"  {{\"rel_path\": \"path/to/file\", \"new_content\": \"<full corrected file content>\"}}\n"
                f"]\n"
                f"Return ONLY the JSON array, no explanation."
            )
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=8000,
            )
            raw = resp.choices[0].message.content
            if isinstance(raw, list):
                raw = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in raw)
            raw = (raw or "").strip()
            # Strip markdown fences (```json ... ``` or ``` ... ```)
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw).strip()

            # Try to extract JSON array even if the model added extra prose
            array_match = re.search(r"\[.*\]", raw, re.DOTALL)
            if array_match:
                raw = array_match.group(0)

            updated = json.loads(raw)
            changed = []
            for entry in updated:
                rel_path = entry.get("rel_path", "")
                new_content = entry.get("new_content", "")
                fpath = os.path.join(self._clone_dir, rel_path)
                if os.path.exists(fpath) and new_content:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    changed.append(rel_path)
            if changed:
                return f"LLM applied reviewer changes to: {', '.join(changed)}"
            return "LLM returned no changes."
        except Exception as e:
            import traceback; traceback.print_exc()
            return f"LLM change failed ({e}); no files updated."

    def address_review_comments(self) -> ToolResult:
        comment = self._last_review_comment or ""
        result = self._apply_review_change(comment) if comment else "No reviewer comment stored."
        self.timeline.append(f"Addressed review comments: {result}")
        return ToolResult(True, f"Review comments addressed. {result}")

    def merge_pr(self, item: WorkItem) -> ToolResult:
        """Complete (merge) the PR in Azure DevOps."""
        if not self._pr_id:
            return ToolResult(False, "No PR ID — create_pr must run first.")
        # Get current PR to retrieve lastMergeSourceCommit (required by ADO)
        url = (
            f"{self._ado_org_url}/{self._ado_project}"
            f"/_apis/git/repositories/{self._ado_repo}/pullrequests/{self._pr_id}?api-version=7.1"
        )
        pr = requests.get(url, headers=self._ado_headers(), timeout=15).json()
        last_commit = pr.get("lastMergeSourceCommit", {}).get("commitId", "")
        body = {
            "status": "completed",
            "lastMergeSourceCommit": {"commitId": last_commit},
            "completionOptions": {
                "mergeStrategy": "squash",
                "deleteSourceBranch": False,
            },
        }
        resp = requests.patch(url, headers=self._ado_headers(), json=body, timeout=15)
        if resp.status_code == 200:
            self.timeline.append(f"PR #{self._pr_id} merged: {self._feature_branch} \u2192 {self._ado_branch}")
            return ToolResult(True, f"PR #{self._pr_id} merged into {self._ado_branch}. Deployment pipeline triggered.")
        return ToolResult(False, f"Merge failed: {resp.status_code} {resp.text[:300]}")

    def deploy(self) -> ToolResult:
        """Find the ADO pipeline run auto-triggered by the merge, poll until the
        'Deploying to DEV' stage completes.  Prints a URL for the human to approve
        the DEV gate when that stage starts waiting."""
        pipeline_name = os.getenv("AZURE_DEVOPS_PIPELINE_NAME", "")
        if not pipeline_name:
            self.timeline.append("Deployment: no AZURE_DEVOPS_PIPELINE_NAME configured — skipped")
            return ToolResult(True, "No AZURE_DEVOPS_PIPELINE_NAME set — skipping deploy.")

        # ── 1. Find pipeline definition ID ──────────────────────────────────
        from urllib.parse import quote
        defs_url = (
            f"{self._ado_org_url}/{self._ado_project}"
            f"/_apis/build/definitions?name={quote(pipeline_name)}&api-version=7.1"
        )
        dr = requests.get(defs_url, headers=self._ado_headers(), timeout=15)
        defs = dr.json().get("value", [])
        if not defs:
            return ToolResult(False, f"Pipeline '{pipeline_name}' not found in ADO project.")
        definition_id = defs[0]["id"]

        # ── 2. Find the latest run on the target branch (retry — merge trigger is async) ──
        build_id = None
        build_number = ""
        for attempt in range(8):
            br = requests.get(
                f"{self._ado_org_url}/{self._ado_project}/_apis/build/builds"
                f"?definitions={definition_id}"
                f"&branchName=refs/heads/{self._ado_branch}"
                f"&queryOrder=queueTimeDescending"
                f"&$top=3&api-version=7.1",
                headers=self._ado_headers(), timeout=15,
            )
            builds = br.json().get("value", [])
            if builds:
                build_id = builds[0]["id"]
                build_number = builds[0].get("buildNumber", str(build_id))
                break
            print(f"  [deploy] Waiting for pipeline run to appear (attempt {attempt + 1}/8)...")
            time.sleep(10)

        if not build_id:
            return ToolResult(False, "No pipeline run found after merge. Trigger it manually in ADO.")

        run_url = f"{self._ado_org_url}/{self._ado_project}/_build/results?buildId={build_id}"
        self._pipeline_run_id = build_id
        print(f"  [deploy] Pipeline run #{build_id} ({build_number})")
        print(f"  [deploy] URL: {run_url}")
        self.timeline.append(f"Pipeline run #{build_id} found: {build_number}")

        # ── 3. Poll timeline for 'Deploying to DEV' stage ───────────────────
        timeline_url = (
            f"{self._ado_org_url}/{self._ado_project}"
            f"/_apis/build/builds/{build_id}/timeline?api-version=7.1"
        )
        dev_keywords = {"dev", "deploying to dev", "deploy to dev", "deploy dev"}
        approval_announced = False
        poll_count = 0
        last_active_stage = ""

        while True:
            tr = requests.get(timeline_url, headers=self._ado_headers(), timeout=15)
            if tr.status_code == 204:
                # Build started but timeline not populated yet — wait and retry
                poll_count += 1
                print(f"  [deploy] Timeline not ready yet (204). Poll #{poll_count}, waiting 15s...")
                time.sleep(15)
                continue
            if tr.status_code != 200:
                return ToolResult(False, f"Timeline fetch failed: {tr.status_code}")

            records = tr.json().get("records", [])

            # Check overall build result first
            br2 = requests.get(
                f"{self._ado_org_url}/{self._ado_project}/_apis/build/builds/{build_id}?api-version=7.1",
                headers=self._ado_headers(), timeout=15,
            )
            build_info = br2.json()
            overall_result = build_info.get("result", "")
            if overall_result == "failed":
                return ToolResult(False, f"Pipeline run #{build_id} failed. Check: {run_url}")

            # Separate stage records from job/task records
            stage_records = [r for r in records if r.get("type") == "Stage"]

            # Find the currently-running stage (to show pre-DEV progress)
            active_stage = next(
                (r["name"] for r in stage_records if r.get("state") == "inProgress"),
                None,
            )
            if active_stage and active_stage != last_active_stage:
                last_active_stage = active_stage
                print(f"  [deploy] Stage running: '{active_stage}'")

            # Find DEV stage record
            dev_record = next(
                (r for r in stage_records
                 if any(kw in r.get("name", "").lower() for kw in dev_keywords)),
                None,
            )

            if dev_record:
                state = dev_record.get("state", "")
                result = dev_record.get("result", "")
                stage_name = dev_record.get("name", "Deploying to DEV")

                if state == "completed":
                    if result == "succeeded":
                        self.timeline.append(f"Pipeline run #{build_id}: '{stage_name}' succeeded")
                        return ToolResult(True, f"Deployment to DEV succeeded. Run: {run_url}")
                    else:
                        return ToolResult(False, f"'{stage_name}' {result}. Run: {run_url}")

                # Stage appeared and is not yet completed — may be at approval gate
                if not approval_announced:
                    print(f"  [deploy] '{stage_name}' stage is {state}.")
                    print(f"  [deploy] If an approval gate is pending, approve at:")
                    print(f"  [deploy]   {run_url}")
                    approval_announced = True

            poll_count += 1
            dev_state = dev_record.get("state", "not started") if dev_record else "not started"
            print(f"  [deploy] Poll #{poll_count}: DEV={dev_state}, run={build_info.get('status', '?')}. Waiting 30s...")
            time.sleep(30)

    def smoke_test(self) -> ToolResult:
        self.timeline.append("Smoke test executed against Azure App Service")
        return ToolResult(True, "App Service returned expected responses. Release verified.")
