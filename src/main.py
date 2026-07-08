from __future__ import annotations

from dotenv import load_dotenv

from agent_core import LLMClient
from tools import DevWorkflowTools


def run_learning_agent() -> None:
    load_dotenv()

    tools = DevWorkflowTools()
    llm = LLMClient()

    item = tools.get_work_item()
    print(f"Working on: {item.key} - {item.title}")

    step = 0
    completed_actions: list[str] = []
    while True:
        decision = llm.decide_next_action(item, completed_actions)
        print(f"\nStep {step + 1}: {decision.action}")
        print(f"Reason : {decision.reason}")

        if decision.action == "analyze_story":
            result = tools.analyze_story(item)
        elif decision.action == "create_feature_branch":
            result = tools.create_feature_branch(item)
        elif decision.action == "apply_code_change":
            result = tools.apply_code_change()
        elif decision.action == "run_tests":
            result = tools.run_tests()
        elif decision.action == "commit_and_push":
            result = tools.commit_and_push(item)
        elif decision.action == "create_pr":
            result = tools.create_pr(item)
        elif decision.action == "wait_for_review":
            result = tools.wait_for_review()
            print(f"Tool output: {result.message}")
            if not result.ok:
                print("  -> Review not approved. Agent will address comments and re-submit.")
                completed_actions.append("wait_for_review:changes_requested")
            else:
                print("  -> PR approved. Proceeding to merge.")
                completed_actions.append("wait_for_review:approved")
            step += 1
            continue
        elif decision.action == "address_review_comments":
            result = tools.address_review_comments()
            print(f"Tool output: {result.message}")
            if not result.ok:
                print("Stopping because a tool failed.")
                break
            completed_actions.append("address_review_comments")
            step += 1
            # Always run tests then commit after addressing review comments —
            # don't let the LLM skip these steps or the vote never gets reset.
            print(f"\nStep {step + 1}: run_tests  [enforced post-review sequence]")
            # Evaluate purely against the story — no reviewer_comment override.
            # If reviewer's change aligns with the story, tests pass.
            # If not, we re-fetch the story (reviewer may have updated it) and try once more.
            result = tools.run_tests()
            print(f"Tool output: {result.message}")
            if not result.ok:
                # Tests failed — reviewer's change may contradict the story acceptance criteria.
                # Re-fetch the Jira story: if the reviewer also updated it, tests should now pass.
                print("  [post-review] Tests failed — re-fetching story in case requirements were updated...")
                item = tools.get_work_item()
                tools._current_item = item
                # Still no reviewer_comment — story must be the source of truth.
                result = tools.run_tests()
                print(f"  [post-review] Re-run result: {result.message}")
                if not result.ok:
                    print("\nStopping — tests still fail after story refresh.")
                    print("The reviewer's comment contradicts the current story acceptance criteria.")
                    print("Action required: Update the Jira story to match the reviewer's feedback, then re-run.")
                    break
            completed_actions.append("run_tests")
            step += 1
            print(f"\nStep {step + 1}: commit_and_push  [enforced post-review sequence]")
            result = tools.commit_and_push(item)
            print(f"Tool output: {result.message}")
            if not result.ok:
                print("Stopping because push failed after addressing review.")
                break
            completed_actions.append("commit_and_push")
            step += 1
            continue
        elif decision.action == "merge_pr":
            result = tools.merge_pr(item)
        elif decision.action == "deploy":
            result = tools.deploy()
        elif decision.action == "smoke_test":
            result = tools.smoke_test()
        elif decision.action == "done":
            print("Agent finished the workflow.")
            break
        else:
            print(f"Unknown action: {decision.action}")
            break

        print(f"Tool output: {result.message}")
        if not result.ok:
            print("Stopping because a tool failed.")
            break

        completed_actions.append(decision.action)

        step += 1

    print("\nTimeline:")
    for event in tools.timeline:
        print(f"- {event}")


if __name__ == "__main__":
    run_learning_agent()
