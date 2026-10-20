"""Deterministic, repository-stratified sampling before any paid inference."""

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

from scripts.swebench.common import config, save_json

SEED = "codehead-lite20-v1"


def select_tasks(rows, excluded, count=20):
    groups = defaultdict(list)
    for row in rows:
        if row["instance_id"] not in excluded:
            groups[row["repo"]].append(row["instance_id"])
    repos = sorted(groups)
    if not len(repos) <= count <= sum(map(len, groups.values())):
        raise ValueError(
            "Sample must cover every repo without exceeding available tasks"
        )
    if any(len(ids) != len(set(ids)) for ids in groups.values()):
        raise ValueError("Duplicate candidate instance IDs")
    remaining = count - len(repos)
    capacities = {repo: len(groups[repo]) - 1 for repo in repos}
    total = sum(capacities.values())
    quotas = {
        repo: 1 + (remaining * capacities[repo] // total if total else 0)
        for repo in repos
    }
    remainders = sorted(
        repos,
        key=lambda repo: (
            -(remaining * capacities[repo] % total if total else 0),
            repo,
        ),
    )
    for repo in remainders[: count - sum(quotas.values())]:
        quotas[repo] += 1
    chosen = {
        repo: sorted(
            groups[repo],
            key=lambda instance: (
                hashlib.sha256((SEED + "\0" + instance).encode()).hexdigest(),
                instance,
            ),
        )[: quotas[repo]]
        for repo in repos
    }
    ordered = [
        chosen[repo][index]
        for index in range(max(quotas.values()))
        for repo in repos
        if index < len(chosen[repo])
    ]
    return ordered, {
        "seed": SEED,
        "excluded_instance_ids": sorted(excluded),
        "candidate_counts": dict(
            sorted(Counter({repo: len(groups[repo]) for repo in repos}).items())
        ),
        "quotas": quotas,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "metadata", type=Path, help="Identity metadata from the fixed dataset revision"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError(
            "Frozen configuration already exists; do not silently replace it"
        )
    settings = config()
    rows = json.loads(args.metadata.read_text("utf-8"))
    ids, selection = select_tasks(rows, set(settings["instance_ids"]))
    selection["metadata_sha256"] = hashlib.sha256(
        args.metadata.read_bytes()
    ).hexdigest()
    settings.update(
        instance_ids=ids,
        selection=selection,
        max_steps=100,
        max_requests=125,
        pricing_checked_date="2026-10-20",
    )
    save_json(args.output, settings)


if __name__ == "__main__":
    main()
