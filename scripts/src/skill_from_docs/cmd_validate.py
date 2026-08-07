"""openapi-harvest validate — completion check for a harvested workspace."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from ._manifest import now_iso, verify_hashes
from ._provenance import find_all_provenance
from ._schema import lint_handoff


def add_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "validate",
        help="completion check (local-by-default)",
        description="Verify a harvested workspace is complete (docs.md, handoff.json, provenance, hashes).",
    )
    p.add_argument("workspace", nargs="?")
    p.add_argument("--strict", action="store_true")
    p.add_argument("--network", action="store_true")
    p.add_argument(
        "--allow-host",
        action="append",
        default=[],
        help="host allowed for --network re-fetches (repeatable; required with --network)",
    )
    p.add_argument("--json", dest="json_out", action="store_true")
    p.set_defaults(func=run)


def _add_check(checks: list[dict[str, Any]], cid: str, passed: bool, message: str | None, severity: str = "error") -> None:
    checks.append({"id": cid, "passed": passed, "message": message, "severity": severity})


def _extract_sections(text: str) -> list[tuple[int, str, str]]:
    """Return (line_no, level, title) for every H2/H3 heading."""
    out = []
    for i, line in enumerate(text.splitlines(), start=1):
        if line.startswith("### "):
            out.append((i, "h3", line[4:].strip()))
        elif line.startswith("## "):
            out.append((i, "h2", line[3:].strip()))
    return out


def _section_has_provenance(text: str, line_idx: int, lines: list[str]) -> bool:
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


def run(args) -> int:
    # An empty HostAllowlist allows everything, so --network without
    # --allow-host would silently be the unrestricted GET this gate exists to
    # prevent. Same policy as `auth` and `probe`.
    if getattr(args, "network", False) and not getattr(args, "allow_host", None):
        print(
            "ERROR: --network requires --allow-host HOST (repeatable). The URL is read "
            "from handoff.json, which this command did not produce.",
            file=sys.stderr,
        )
        return 1

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
        ok = _section_has_provenance(docs_text, line_no, lines)
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

    # 5. Every file in raw/ and probes/ is referenced
    referenced_files: set[str] = set()
    for entry in find_all_provenance(docs_text):
        for field_name in ("raw_file", "fixture"):
            if field_name in entry.fields:
                referenced_files.add(entry.fields[field_name])

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
                _add_check(
                    checks,
                    f"orphan_capture_{rel.replace('/', '_')}",
                    False,
                    f"file {rel} is not referenced by any provenance comment",
                )

    # 6. Provenance comments' local file paths resolve
    for entry in find_all_provenance(docs_text):
        for field_name in ("raw_file", "fixture"):
            if field_name in entry.fields:
                rel = entry.fields[field_name]
                full = os.path.join(workspace, rel)
                if not os.path.exists(full):
                    _add_check(
                        checks,
                        f"missing_provenance_target_{rel.replace('/', '_')}",
                        False,
                        f"provenance references missing file: {rel}",
                    )

    # 7. manifest.json hash verify
    manifest_path = os.path.join(workspace, "manifest.json")
    if os.path.exists(manifest_path):
        failures = verify_hashes(workspace)
        if failures:
            for f in failures:
                _add_check(checks, f"manifest_hash_{hash(f) & 0xffff:x}", False, f)
        else:
            _add_check(checks, "manifest_hash_verify", True, None)
    else:
        _add_check(checks, "manifest_exists", False, "manifest.json missing")

    # 8. archetype-4 signals
    if handoff_ok:
        signals = handoff.get("content_shape_signals") or {}
        archetype = handoff.get("archetype_primary")
        if archetype == 4:
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
            if not signals.get("spec_url"):
                warnings.append(
                    {
                        "id": "archetype4_warn_spec_url",
                        "passed": False,
                        "message": "optional archetype-4 signal absent: spec_url",
                        "severity": "warn",
                    }
                )
            for opt_key in ("spec_format", "tag_count"):
                if not signals.get(opt_key):
                    warnings.append(
                        {
                            "id": f"archetype4_warn_{opt_key}",
                            "passed": False,
                            "message": f"optional archetype-4 signal absent: {opt_key}",
                            "severity": "warn",
                        }
                    )

    # 9. provenance_index covers every section
    if handoff_ok:
        provenance_index = handoff.get("provenance_index", {})
        # For H3 sections under API reference, check that provenance_index has them
        missing_index_sections: list[str] = []
        for _l, lvl, t in sections:
            if lvl != "h3":
                continue
            if not any(t in key for key in provenance_index.keys()):
                missing_index_sections.append(t)
        if missing_index_sections:
            for s in missing_index_sections:
                warnings.append(
                    {
                        "id": f"provenance_index_missing_{s}",
                        "passed": False,
                        "message": f"provenance_index missing section '{s}'",
                        "severity": "warn",
                    }
                )
        else:
            _add_check(checks, "provenance_index_coverage", True, None)

    # 10. coverage_checklist URLs exist in provenance_index
    if handoff_ok:
        cc = handoff.get("coverage_checklist") or []
        provenance_urls = set()
        for v in (handoff.get("provenance_index") or {}).values():
            for s in v.get("sources", []):
                if s.get("url"):
                    provenance_urls.add(s["url"])
        if isinstance(cc, list):
            for item in cc:
                if isinstance(item, dict) and item.get("source") and item["source"] not in provenance_urls:
                    warnings.append(
                        {
                            "id": "coverage_checklist_unknown_source",
                            "passed": False,
                            "message": f"coverage_checklist references unknown source: {item['source']}",
                            "severity": "warn",
                        }
                    )

    # Optional network check
    if args.network and handoff_ok:
        try:
            from ._http import AllowlistViolation, HostAllowlist, build_client

            # spec_url is read out of a local handoff.json, which is data this
            # command did not produce. Gate it like every other outbound call
            # rather than GETting whatever the file happens to name.
            allowlist = HostAllowlist(args.allow_host)
            spec_url = (handoff.get("content_shape_signals") or {}).get("spec_url")
            urls_to_check = [spec_url] if spec_url else []
            with build_client(timeout=10.0) as client:
                for url in urls_to_check:
                    try:
                        allowlist.check(url)
                        r = client.get(url)
                        ok = r.status_code == 200
                        _add_check(
                            checks,
                            f"network_{hash(url) & 0xffff:x}",
                            ok,
                            None if ok else f"URL {url} returned {r.status_code}",
                        )
                    except AllowlistViolation as e:
                        _add_check(checks, "network_allowlist", False, str(e))
                    except Exception as e:
                        _add_check(checks, "network_error", False, f"can't fetch {url}: {e}")
        except RuntimeError:
            pass

    # Compute verdict
    failed = [c for c in checks if not c["passed"]]
    if args.strict:
        # Promote warnings to errors
        for w in warnings:
            failed.append(w)
    verdict = "pass" if not failed else "fail"
    if failed and not args.strict and not any(c["severity"] == "error" for c in failed):
        verdict = "warn"

    summary = (
        f"Pass: {sum(1 for c in checks if c['passed'])}/{len(checks)}, "
        f"warn: {len(warnings)}, fail: {sum(1 for c in checks if not c['passed'])}"
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
            mark = "OK  " if c["passed"] else "FAIL"
            extra = f" — {c['message']}" if c.get("message") else ""
            print(f"  {mark} {c['id']}{extra}")
        for w in warnings:
            print(f"  WARN {w['id']} — {w.get('message')}")

    if verdict == "fail":
        return 1
    if verdict == "warn" and args.strict:
        return 1
    return 0
