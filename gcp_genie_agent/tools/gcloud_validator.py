"""Deterministic gcloud command *syntax* validator (function #2).

Validation is done against gcloud's own static command/flag tree
(``gcloud_completions.py``, shipped inside the Cloud SDK and bundled into this
package). It needs no ``gcloud`` binary at runtime, so it works inside the
Agent Engine sandbox.

What it checks deterministically:
  * the command starts with ``gcloud``
  * the command group / sub-command path actually exists
  * every ``--flag`` is a real flag for that command (or a global flag)

What it does NOT check: semantic correctness of flag *values* (e.g. whether a
zone or machine type exists). It is a syntax/spelling gate, not an executor.
"""

from __future__ import annotations

import shlex
from functools import lru_cache
from typing import Any


@lru_cache(maxsize=1)
def _tree() -> dict:
    # Imported lazily: the module is a ~6.6MB dict literal. It is sourced from the
    # local Cloud SDK by deploy.sh (and is git-ignored), so it is always present
    # in the deployed package but may be absent in a fresh checkout.
    try:
        from ..data import gcloud_completions
    except ImportError as exc:  # pragma: no cover - only in a bare checkout
        raise RuntimeError(
            "gcp_genie_agent/data/gcloud_completions.py is missing. It is bundled "
            "from your local Cloud SDK by deploy.sh. To populate it manually, run: "
            "cp \"$(gcloud info --format='value(installation.sdk_root)')/data/cli/"
            "gcloud_completions.py\" gcp_genie_agent/data/"
        ) from exc

    return gcloud_completions.STATIC_COMPLETION_CLI_TREE


def _flag_type(name: str, flags: dict) -> str | None:
    """Return the flag's value type ('value'|'bool'|'dynamic') or None if unknown.

    Handles boolean ``--no-foo`` negation of a known ``--foo`` bool flag.
    """
    if name in flags:
        return flags[name]
    if name.startswith("--no-"):
        base = "--" + name[5:]
        if flags.get(base) == "bool":
            return "bool"
    return None


def validate_gcloud_command(command: str) -> dict[str, Any]:
    """Validate the syntax of a gcloud command without executing it.

    Use this on every gcloud command you generate before showing it to the user.
    If ``valid`` is false, fix the command based on ``errors`` and validate again.

    Args:
        command: A single gcloud command line, e.g.
            "gcloud compute instances create vm1 --zone=us-central1-a --machine-type=e2-medium".

    Returns:
        A dict with:
          valid (bool): True only if no errors were found.
          resolved_command (str): the command/group path that was recognized.
          errors (list[str]): hard syntax problems (unknown command/flag).
          warnings (list[str]): non-fatal notes.
          unknown_flags (list[str]): flags that don't exist for this command.
          validated_flags (list[str]): flags confirmed to exist.
    """
    result: dict[str, Any] = {
        "command": command,
        "valid": False,
        "resolved_command": None,
        "errors": [],
        "warnings": [],
        "unknown_flags": [],
        "validated_flags": [],
    }

    try:
        tokens = shlex.split(command, comments=False)
    except ValueError as exc:
        result["errors"].append(f"Could not parse command: {exc}")
        return result

    if not tokens:
        result["errors"].append("Empty command.")
        return result
    if tokens[0] != "gcloud":
        result["errors"].append("Command must start with 'gcloud'.")
        return result
    tokens = tokens[1:]

    tree = _tree()
    node = tree
    path: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.startswith("-"):
            break
        cmds = node.get("commands", {})
        if tok in cmds:
            node = cmds[tok]
            path.append(tok)
            i += 1
        else:
            break

    result["resolved_command"] = "gcloud " + " ".join(path) if path else "gcloud"

    if not path:
        first = tokens[0] if tokens else ""
        if first and not first.startswith("-"):
            result["errors"].append(f"Unknown command group: '{first}'.")
        else:
            result["errors"].append("No command specified after 'gcloud'.")
        return result

    cmds = node.get("commands", {})
    if cmds:
        # Resolved to a command *group*, not a runnable leaf command.
        sample = ", ".join(sorted(cmds)[:15])
        if i < len(tokens) and not tokens[i].startswith("-"):
            result["errors"].append(
                f"Unknown sub-command '{tokens[i]}' under '{result['resolved_command']}'. "
                f"Valid sub-commands include: {sample}."
            )
        else:
            result["errors"].append(
                f"Incomplete command: '{result['resolved_command']}' is a command group, "
                f"not a runnable command. Expected one of: {sample}."
            )
        return result

    # Leaf command: validate flags against command flags + global flags.
    flags = dict(tree.get("flags", {}))
    flags.update(node.get("flags", {}))

    rest = tokens[i:]
    j = 0
    while j < len(rest):
        tok = rest[j]
        if tok.startswith("--"):
            name, has_eq, _ = tok.partition("=")
            ftype = _flag_type(name, flags)
            if ftype is None:
                result["unknown_flags"].append(name)
            else:
                result["validated_flags"].append(name)
                # Consume a following value token so it isn't misread as a flag.
                if (
                    not has_eq
                    and ftype in ("value", "dynamic")
                    and j + 1 < len(rest)
                    and not rest[j + 1].startswith("--")
                ):
                    j += 1
        elif tok.startswith("-") and len(tok) > 1 and tok not in ("-h",):
            result["unknown_flags"].append(tok)
        # else: positional argument — not validated.
        j += 1

    if result["unknown_flags"]:
        result["errors"].append(
            f"Unknown or misspelled flag(s) for '{result['resolved_command']}': "
            f"{', '.join(result['unknown_flags'])}."
        )

    result["valid"] = not result["errors"]
    return result
