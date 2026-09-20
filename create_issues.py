#!/usr/bin/env python3
"""
Bulk-create feature, story, and task issues on GitHub from backlog.json.

Three levels, nested with GitHub sub-issues:
    Feature  ->  Story  ->  Task

Safe to re-run: issues are matched by title, and anything already there is
skipped instead of duplicated. If it dies halfway, run it again.

Usage:
    export GITHUB_TOKEN=github_pat_...
    python create_issues.py OWNER/REPO                      # dry run
    python create_issues.py OWNER/REPO --apply              # create everything
    python create_issues.py OWNER/REPO --sprint 1 --apply   # sprint 1 only
    python create_issues.py OWNER/REPO --sprint 1 --apply --no-tasks

Token needs Issues: Read and write on the repo (fine-grained PAT), or the
classic `repo` scope.
"""

import argparse
import json
import os
import sys
import time

import requests

API = "https://api.github.com"
VERSION = "2022-11-28"

LABELS = [
    ("feature", "0e8a16", "Capability area. stories hang off it"),
    ("story", "1d76db", "User story; a vertical slice"),
    ("task", "c5def5", "Piece of a story; grab one"),
    ("sprint-1", "5319e7", "Committed to sprint 1"),
    ("sprint-2", "b4a8f5", "Planned for sprint 2"),
    ("sprint-3", "ededed", "Backlog / later sprint"),
]

class GitHub:
    def __init__(self, token, repo):
        self.repo = repo
        self.s = requests.Session()
        self.s.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": VERSION,
            }
        )

    def _call(self, method, path, **kw):
        url = path if path.startswith("http") else f"{API}{path}"
        for attempt in range(4):
            r = self.s.request(method, url, timeout=30, **kw)

            # Secondary rate limits are the ones that actually bite.
            if r.status_code in (403, 429):
                wait = int(r.headers.get("Retry-After", 2 ** (attempt + 3)))
                print(f"  rate limited, sleeping {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                raise SystemExit(f"--> {method} {url} -> {r.status_code}: {r.text[:400]}")
            return r
        raise SystemExit("gave up after repeated rate limiting")

    def existing_issues(self):
        """Title -> issue dict, open and closed. Pull requests excluded."""
        out, url = {}, f"{API}/repos/{self.repo}/issues?state=all&per_page=100"
        while url:
            r = self._call("GET", url)
            for issue in r.json():
                if "pull_request" in issue:  # every PR is also an issue
                    continue
                out[issue["title"]] = issue
            url = r.links.get("next", {}).get("url")
        return out

    def ensure_label(self, name, color, description):
        print(f"Ensure lable is created. Label check: {name} {color} {description}")
        r = self.s.get(f"{API}/repos/{self.repo}/labels/{name}", timeout=30)
        if r.status_code == 200:
            return
        self._call(
            "POST",
            f"/repos/{self.repo}/labels",
            json={"name": name, "color": color, "description": description},
        )
        print(f"  label created: {name}")

    def ensure_milestone(self, title, due_on=None):
        r = self._call("GET", f"/repos/{self.repo}/milestones?state=all&per_page=100")
        for m in r.json():
            if m["title"] == title:
                return m["number"]
        payload = {"title": title}
        if due_on:
            payload["due_on"] = due_on
        m = self._call("POST", f"/repos/{self.repo}/milestones", json=payload)
        print(f"  milestone created: {title}")
        return m.json()["number"]

    def create_issue(self, title, body, labels, milestone=None):
        payload = {"title": title, "body": body, "labels": labels}
        if milestone:
            payload["milestone"] = milestone
        issue = self._call("POST", f"/repos/{self.repo}/issues", json=payload).json()
        time.sleep(1)  # creating issues too fast trips the abuse detector
        return issue

    def link_sub_issue(self, parent_number, child_id):
        """Sub-issues take the child's global id, not its per-repo number."""
        self._call(
            "POST",
            f"/repos/{self.repo}/issues/{parent_number}/sub_issues",
            json={"sub_issue_id": child_id},
        )
        time.sleep(0.5)


def wanted(story, sprint_filter):
    return sprint_filter is None or story.get("sprint") == sprint_filter


def dry_run(backlog, sprint_filter, include_tasks):
    print("DRY RUN — nothing will be created. Add --apply to execute.\n")
    features = stories = tasks = 0
    for feat in backlog["features"]:
        picked = [s for s in feat["stories"] if wanted(s, sprint_filter)]
        if not picked and sprint_filter is not None:
            continue
        features += 1
        print(feat["title"])
        for s in picked:
            stories += 1
            print(f"  story  [sprint {s.get('sprint', '-')}]  {s['title']}")
            if include_tasks:
                for t in s.get("tasks", []):
                    tasks += 1
                    print(f"    task   {t['title']}")
        print()
    print(f"{features} features, {stories} stories, {tasks} tasks "
          f"= {features + stories + tasks} issues")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("repo", help="OWNER/REPO")
    p.add_argument("--backlog", default="backlog.json")
    p.add_argument("--apply", action="store_true", help="create for real")
    p.add_argument("--sprint", type=int, help="only this sprint's stories")
    p.add_argument("--no-tasks", action="store_true", help="features and stories only")
    args = p.parse_args()

    with open(args.backlog) as f:
        backlog = json.load(f)

    include_tasks = not args.no_tasks

    if not args.apply:
        dry_run(backlog, args.sprint, include_tasks)
        return

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("set GITHUB_TOKEN first")

    gh = GitHub(token, args.repo)

    print("Setting up labels and milestone...")
    for name, color, desc in LABELS:
        gh.ensure_label(name, color, desc)
    milestone = gh.ensure_milestone(
        backlog.get("milestone", "Sprint 1"), backlog.get("milestone_due")
    )

    print("Reading existing issues...")
    existing = gh.existing_issues()
    created = skipped = 0

    def get_or_create(title, body, labels, milestone_num=None, parent=None):
        nonlocal created, skipped
        issue = existing.get(title)
        if issue:
            skipped += 1
            return issue, False
        issue = gh.create_issue(title, body, labels, milestone=milestone_num)
        existing[title] = issue
        created += 1
        if parent:
            gh.link_sub_issue(parent["number"], issue["id"])
        return issue, True

    for feat in backlog["features"]:
        picked = [s for s in feat["stories"] if wanted(s, args.sprint)]
        if not picked and args.sprint is not None:
            continue

        parent, is_new = get_or_create(
            feat["title"], feat["body"], feat.get("labels", ["feature"])
        )
        print(f"{'+' if is_new else '='} {feat['title']}  (#{parent['number']})")

        for story in picked:
            sprint = story.get("sprint")
            labels = ["story"] + ([f"sprint-{sprint}"] if sprint else [])
            s_issue, is_new = get_or_create(
                story["title"],
                story["body"],
                labels,
                milestone_num=milestone if sprint == 1 else None,
                parent=parent,
            )
            print(f"  {'+' if is_new else '='} {story['title']}  (#{s_issue['number']})")

            if not include_tasks:
                continue
            for task in story.get("tasks", []):
                t_labels = ["task"] + ([f"sprint-{sprint}"] if sprint else [])
                t_issue, is_new = get_or_create(
                    task["title"],
                    task["body"],
                    t_labels,
                    milestone_num=milestone if sprint == 1 else None,
                    parent=s_issue,
                )
                print(f"    {'+' if is_new else '='} {task['title']}  (#{t_issue['number']})")

    print(f"\nDone. {created} created, {skipped} already there.")


if __name__ == "__main__":
    main()
