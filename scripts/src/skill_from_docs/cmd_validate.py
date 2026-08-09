"""openapi-harvest validate — completion check for a harvested workspace."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from typing import Any

from . import _cli
from ._manifest import now_iso, record_run, verify_hashes
from ._http import require_allowlist
from ._provenance import find_all_provenance
from ._redaction import redact_text
from ._schema import lint_handoff, read_fetch_url


# The `verdict` values `validate --json` can emit. scripts/README.md documents
# this as a stable v1 contract CI consumers may assert on, so the two must not
# drift — `test_cmd_validate.py` asserts the README lists exactly these.
VERDICTS: tuple[str, ...] = ("pass", "warn", "fail")


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "validate",
        help="completion check (local-by-default)",
        description="Verify a harvested workspace is complete (docs.md, handoff.json, provenance, hashes).",
        parents=[_cli.allow_host()],
    )
    p.add_argument("workspace", nargs="?")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--network", action="store_true")
    p.add_argument("--json", dest="json_out", action="store_true")
    p.set_defaults(func=run)


def _add_check(checks: list[dict[str, Any]], cid: str, passed: bool, message: str | None, severity: str = "error") -> None:
    checks.append({"id": cid, "passed": passed, "message": message, "severity": severity})


def _id_suffix(value: str) -> str:
    """Stable short digest for per-item check ids.

    Must not be Python's `hash()`: that is salted per process for str, so the
    same file produced a different `manifest_hash_*` id on every run. Check
    `id` is part of the documented `--json` contract, so a consumer could not
    match on those ids at all.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:4]


def _extract_sections(text: str) -> list[tuple[int, str, str]]:
    """Return (line_no, level, title) for every H2/H3 heading."""
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.startswith("### "):
            out.append((i, "h3", line[4:].strip()))
        elif line.startswith("## "):
            out.append((i, "h2", line[3:].strip()))
    return out


def _section_has_provenance(line_idx: int, lines: list[str]) -> bool:
    """Look forward from `line_idx` (1-indexed) until the next heading or EOF;
    return True if any provenance comment OR `_Not documented upstream._` is found.
    """
    n = len(lines)
    i = line_idx  # next line after heading is index `line_idx` in 0-based
    found = False
    while i < n:
        line = lines[i]
        if line.startswith(("## ", "### ", "#### ")):
            break
        if "_Not documented upstream._" in line:
            found = True
        if "<!--" in line and ("source:" in line or "probe:" in line):
            found = True
        i += 1
    return found


def _warn(warnings: list[dict[str, Any]], wid: str, message: str) -> None:
    """Append to the advisory channel. Unlike `checks`, this never moves the
    non-strict verdict — see the verdict block in `run`."""
    warnings.append({"id": wid, "passed": False, "message": message, "severity": "warn"})


def _check_archetype4_signals(
    handoff: dict[str, Any], checks: list[dict[str, Any]], warnings: list[dict[str, Any]]
) -> None:
    """Check 8. An archetype-4 workspace must carry a spec with endpoints; the
    remaining signals are recommended, so their absence is advisory only."""
    signals = handoff.get("content_shape_signals") or {}
    if handoff.get("archetype_primary") != 4:
        return
    has_spec = bool(signals.get("has_openapi_spec"))
    endpoint_count = signals.get("endpoint_count") or 0
    _add_check(
        checks,
        "archetype4_has_spec",
        has_spec and endpoint_count >= 1,
        None
        if has_spec and endpoint_count >= 1
        else "archetype-4 requires has_openapi_spec=true and endpoint_count>=1",
    )
    # spec_url is a recommended optional field; absent emits a warning
    for opt_key in ("spec_url", "spec_format", "tag_count"):
        if not signals.get(opt_key):
            _warn(
                warnings,
                f"archetype4_warn_{opt_key}",
                f"optional archetype-4 signal absent: {opt_key}",
            )


def _check_provenance_index_coverage(
    handoff: dict[str, Any],
    sections: list[tuple[int, str, str]],
    checks: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    """Check 9. Every H3 section (the API-reference entries) should appear in
    `provenance_index`, which is what makes skill-creator's downstream
    anti-hallucination check mechanical."""
    provenance_index = handoff.get("provenance_index", {})
    missing = [
        title
        for _line_no, level, title in sections
        if level == "h3" and not any(title in key for key in provenance_index)
    ]
    if not missing:
        _add_check(checks, "provenance_index_coverage", True, None)
        return
    for title in missing:
        _warn(
            warnings,
            f"provenance_index_missing_{title}",
            f"provenance_index missing section '{title}'",
        )


def _check_coverage_checklist_sources(
    handoff: dict[str, Any], warnings: list[dict[str, Any]]
) -> None:
    """Check 10. A coverage_checklist entry naming a source no provenance entry
    records is a claim of coverage nothing backs."""
    checklist = handoff.get("coverage_checklist") or []
    if not isinstance(checklist, list):
        return
    provenance_urls = {
        source["url"]
        for entry in (handoff.get("provenance_index") or {}).values()
        for source in entry.get("sources", [])
        if source.get("url")
    }
    for item in checklist:
        if not isinstance(item, dict):
            continue
        # `consolidate` writes `sources` (a list of URL strings). This read said
        # `source`, singular, so the check never fired on any workspace this tool
        # has ever produced — the same shape of defect as the unreachable `warn`
        # verdict (§A2). Tolerate the singular spelling too rather than assume no
        # hand-written handoff.json uses it.
        raw = item.get("sources")
        sources = raw if isinstance(raw, list) else [raw] if raw else []
        if not sources and item.get("source"):
            sources = [item["source"]]
        for source in sources:
            if isinstance(source, str) and source not in provenance_urls:
                _warn(
                    warnings,
                    "coverage_checklist_unknown_source",
                    f"coverage_checklist references unknown source: {source}",
                )


def _check_network(
    workspace: str,
    handoff: dict[str, Any],
    checks: list[dict[str, Any]],
    network_allowlist,
    transport,
) -> None:
    """Re-fetch the spec URL the workspace records.

    Two URLs, and which one goes where is the whole of A8:

    - The **display** URL, out of `handoff.json`. It is `redact_url`'d, so it is
      safe. Everything that leaves this function carries it and only it: the
      check `id`, every message, and the manifest entry `run` writes afterwards.
    - The **fetchable** URL, out of `raw/source-map.json` via `read_fetch_url`.
      It can carry a live credential. It is passed to `client.get` and nowhere
      else — in particular not into an error string, which is how failure mode 3
      ("credentials travel further than the call that produced them") has bitten
      this repo before. httpx quotes the request URL in its own exception text,
      so `str(e)` goes through `redact_text` rather than being trusted.

    `read_fetch_url` returns a reason instead of a URL when there is nothing
    re-fetchable — a workspace harvested before A8 whose `spec_url` is redacted.
    That is reported as a **passing** check carrying the explanation: the point
    of A8 is that `validate` stops reporting failures that are not real, and a
    skip that flipped the verdict would be the same lie in a new place.
    """
    from ._http import AllowlistViolation, build_client

    # spec_url is read out of a local handoff.json, which is data this command
    # did not produce. Gate it like every other outbound call rather than
    # GETting whatever the file happens to name — the allowlist is bound to the
    # client, so `client.get` is the gate.
    display_url = (handoff.get("content_shape_signals") or {}).get("spec_url")
    if not display_url:
        return

    fetch_url, skip_reason = read_fetch_url(workspace, display_url)
    if fetch_url is None:
        _add_check(checks, f"network_skipped_{_id_suffix(display_url)}", True, skip_reason)
        return

    try:
        with build_client(
            allowlist=network_allowlist, timeout=10.0, transport=transport
        ) as client:
            r = client.get(fetch_url)
            ok = r.status_code == 200
            _add_check(
                checks,
                f"network_{_id_suffix(display_url)}",
                ok,
                None if ok else f"URL {display_url} returned {r.status_code}",
            )
    except AllowlistViolation as e:
        _add_check(checks, "network_allowlist", False, redact_text(str(e)))
    except RuntimeError:  # pragma: no cover - httpx missing
        return
    except Exception as e:
        _add_check(
            checks,
            "network_error",
            False,
            f"can't fetch {display_url}: {redact_text(str(e))}",
        )


def run(args, *, transport=None) -> int:
    # The spec_url is read out of a local handoff.json, which this command did
    # not produce, so --network is gated like every other outbound call.
    network_allowlist = None
    if getattr(args, "network", False):
        network_allowlist = require_allowlist(
            getattr(args, "allow_host", None), subcommand="validate", context="--network"
        )
        if network_allowlist is None:
            return 1

    started = now_iso()
    workspace = args.workspace or os.getcwd()
    checks: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []

    docs_path = os.path.join(workspace, "docs.md")
    handoff_path = os.path.join(workspace, "handoff.json")

    # 1. docs.md exists
    docs_exists = os.path.exists(docs_path) and os.path.getsize(docs_path) > 0
    _add_check(checks, "docs_md_exists", docs_exists, None if docs_exists else "docs.md missing or empty")

    # 2. handoff.json exists + parses
    handoff: dict[str, Any] | None = None
    handoff_ok = False
    if os.path.exists(handoff_path):
        try:
            with open(handoff_path, "r", encoding="utf-8") as f:
                handoff = json.load(f)
            # `handoff_ok` gates the deeper content checks below, so it stays a
            # structural verdict; lint_handoff supplies the specifics instead of
            # the old catch-all "missing required fields".
            shape_problems = lint_handoff(handoff)
            handoff_ok = not shape_problems
            _add_check(
                checks,
                "handoff_json_valid",
                handoff_ok,
                None if handoff_ok else "; ".join(shape_problems),
            )
        except Exception as e:
            _add_check(checks, "handoff_json_valid", False, f"handoff.json parse error: {e}")
    else:
        _add_check(checks, "handoff_json_valid", False, "handoff.json missing")

    # Read docs.md once
    docs_text = ""
    if docs_exists:
        with open(docs_path, "r", encoding="utf-8") as f:
            docs_text = f.read()

    # 3. Every H2 + H3 section has provenance or canonical-empty
    lines = docs_text.splitlines()
    sections = _extract_sections(docs_text)
    canonical_empty_h2 = {"Coverage status", "API reference"}
    for line_no, level, title in sections:
        if level == "h2" and title in canonical_empty_h2:
            continue
        ok = _section_has_provenance(line_no, lines)
        cid = f"{level}_provenance_{title.lower().replace(' ', '_').replace('/', '_').replace(',', '')[:60]}"
        if not ok:
            _add_check(
                checks,
                cid,
                False,
                f"Section '{title}' has no provenance comment or _Not documented upstream._",
            )
        else:
            _add_check(checks, cid, True, None)

    # 4. Every <!-- TODO --> marker has matching gap_list entry
    todos_in_docs = sum(1 for line in lines if "<!-- TODO" in line)
    gap_count = len((handoff or {}).get("gap_list", [])) if handoff_ok else 0
    todos_ok = todos_in_docs <= gap_count
    _add_check(
        checks,
        "todos_match_gap_list",
        todos_ok,
        None if todos_ok else f"{todos_in_docs} TODOs in docs.md but only {gap_count} gap_list entries",
    )

    # 5+6. Every file in raw/ and probes/ is referenced, and every local file a
    # provenance comment references exists. Two questions, one walk: both read
    # the same two fields of the same entries, so the second
    # `find_all_provenance(docs_text)` re-parsed the whole document to ask a
    # different question about an identical list.
    referenced_files: set[str] = set()
    missing_targets: list[str] = []
    for entry in find_all_provenance(docs_text):
        for field_name in ("raw_file", "fixture"):
            if field_name not in entry.fields:
                continue
            ref = entry.fields[field_name]
            referenced_files.add(ref)
            if not os.path.exists(os.path.join(workspace, ref)):
                missing_targets.append(ref)

    for sub in ("raw", "probes"):
        d = os.path.join(workspace, sub)
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            rel = f"{sub}/{name}"
            if rel not in referenced_files and not name.startswith("."):
                # source-map.json + manifest-y files don't need provenance refs
                if name in ("source-map.json",):
                    continue
                # Non-fatal: an unreferenced capture means the harvest left a
                # file behind, not that the workspace is unusable. It is the
                # canonical `warn` — worth surfacing, worth failing under
                # --strict, not worth blocking a handoff over.
                _add_check(
                    checks,
                    f"orphan_capture_{rel.replace('/', '_')}",
                    False,
                    f"file {rel} is not referenced by any provenance comment",
                    severity="warn",
                )

    # Emitted after the orphan-capture checks, and once per reference rather
    # than once per file, so merging the two walks left the `checks` list
    # byte-identical to what the two separate loops produced.
    for ref in missing_targets:
        _add_check(
            checks,
            f"missing_provenance_target_{ref.replace('/', '_')}",
            False,
            f"provenance references missing file: {ref}",
        )

    # 7. manifest.json hash verify
    manifest_path = os.path.join(workspace, "manifest.json")
    if os.path.exists(manifest_path):
        failures = verify_hashes(workspace)
        if failures:
            for f in failures:
                _add_check(checks, f"manifest_hash_{_id_suffix(f)}", False, f)
        else:
            _add_check(checks, "manifest_hash_verify", True, None)
        # `verify_hashes` deliberately checks only the newest digest per path.
        # Reporting the superseded entries as well was tried (A9) and removed
        # after executing it: `consolidate` is not byte-deterministic across
        # runs — every `retrieved:` timestamp moves — so an ordinary re-run
        # leaves an older entry whose digest differs from the file, and the
        # report fired on every one. Under --strict that is a `fail`, which
        # broke the consolidate → validate → re-consolidate → validate --strict
        # sequence CI runs. It also could not have worked: an older entry
        # mismatching is exactly what a legitimate re-run and a tampered file
        # both look like, so the signal carries no information. Read DEFERRED.md
        # §F before re-proposing it.
    else:
        _add_check(checks, "manifest_exists", False, "manifest.json missing")

    # 8, 9, 10. The handoff-content checks. Each one is meaningless without a
    # parsed, shape-valid handoff, so the guard is hoisted here instead of being
    # written out three times; the bodies are functions only so that hoisting
    # does not produce one 60-line `if`.
    if handoff_ok:
        _check_archetype4_signals(handoff, checks, warnings)
        _check_provenance_index_coverage(handoff, sections, checks, warnings)
        _check_coverage_checklist_sources(handoff, warnings)

    # Optional network check
    if args.network and handoff_ok:
        _check_network(workspace, handoff, checks, network_allowlist, transport)

    # Compute verdict from the `checks` list, keyed on severity. `warnings` is
    # a display and --strict channel only: it is populated by "recommended
    # optional field absent", and `spec_url` is legitimately absent for every
    # local-file harvest. Letting it move the non-strict verdict made the
    # ordinary `fetch ./spec.json` -> consolidate -> validate flow report `warn`
    # on a clean workspace, and a verdict that is `warn` for the normal case is
    # a verdict nobody reads.
    errors = [c for c in checks if not c["passed"] and c["severity"] == "error"]
    soft = [c for c in checks if not c["passed"] and c["severity"] != "error"]
    if args.strict:
        # --strict promotes every advisory finding, both channels, to blocking.
        verdict = "fail" if (errors or soft or warnings) else "pass"
    elif errors:
        verdict = "fail"
    elif soft:
        verdict = "warn"
    else:
        verdict = "pass"

    summary = (
        f"Pass: {sum(1 for c in checks if c['passed'])}/{len(checks)}, "
        f"warn: {len(soft) + len(warnings)}, fail: {len(errors)}"
    )

    # Only --network touches the network, and it is the one subcommand whose
    # target host is read out of a workspace file rather than the CLI, so it is
    # the run most worth having in the audit trail. Local runs stay silent —
    # recording them would churn manifest.json on every CI invocation.
    #
    # Guarded on the manifest already existing, because `record_run` writes
    # through `write_manifest`, which `os.makedirs` the workspace. Unguarded,
    # `validate` healed the very thing it was checking: a first run failed
    # `manifest_exists` and created a manifest, so a bare retry of a red CI step
    # went green with nothing fixed. `validate --network /typo` also silently
    # created `/typo`. This command reports on a workspace; it does not build one.
    if args.network and os.path.exists(manifest_path):
        record_run(
            workspace,
            subcommand="validate",
            args={
                "network": True,
                "strict": bool(args.strict),
                "allow_host": sorted(getattr(args, "allow_host", None) or []),
            },
            started_at=started,
            finished_at=now_iso(),
            outputs=[],
        )

    if args.json_out:
        print(json.dumps(
            {
                "workspace": workspace,
                "verdict": verdict,
                "checks": checks,
                "warnings": warnings,
                "summary": summary,
                "checked_at": now_iso(),
            },
            indent=2,
        ))
    else:
        print(f"workspace: {workspace}")
        print(f"verdict:   {verdict}")
        print(summary)
        for c in checks:
            if c["passed"]:
                mark = "OK  "
            else:
                mark = "FAIL" if c["severity"] == "error" else "WARN"
            extra = f" — {c['message']}" if c.get("message") else ""
            print(f"  {mark} {c['id']}{extra}")
        for w in warnings:
            print(f"  WARN {w['id']} — {w.get('message')}")

    # `warn` is advisory: exit 0 so a pipeline keeps going. `--strict` is how
    # you make it blocking, and it does that by producing `fail` above.
    return 1 if verdict == "fail" else 0
