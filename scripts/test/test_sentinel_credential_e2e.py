"""D2's "do instead": an end-to-end sentinel-credential boundary guard.

DEFERRED.md D2 rejected a redaction choke point at the write boundary
(exemptions for `raw/spec.json` would have reintroduced the per-call-site
judgement the choke point was meant to eliminate) and recorded the cheap
version of the same guarantee as the thing to do instead:

    a test that runs a full `fetch -> probe -> consolidate` with a sentinel
    credential in every input position and greps every workspace artifact
    for it. That is the cheap version of the same guarantee, and it would
    have caught the `spec_url` leak.

This is that test. It is entirely offline (httpx.MockTransport, no network),
puts a distinctive sentinel string into every input position a credential can
occupy (URL query string, URL userinfo, request headers, JSON request body,
form-encoded request body, and a nested body key), and then walks every file
the run produced. The sentinel value is unique per position so a failure names
exactly which one leaked.

**It asserts a boundary, not an absence.** A8 gave the workspace exactly one
place a live credential is allowed to land — `raw/source-map.json`'s
`fetch_url`, which is how the audit trail records a URL that can still be
re-fetched after `redact_url` has eaten a benign `?key=petstore`. Every other
file, named explicitly below, must still be clean. Two properties do the work
the old blanket "appears nowhere" assertion used to:

- `EXPECTED_HOLDER` is the only file allowed to contain a sentinel, and only
  at the one key `fetch_url`. A sentinel anywhere else in that same file — or
  in any other file — fails.
- `MUST_NOT_CONTAIN` names `docs.md`, `handoff.json` and every probe fixture
  outright. Those are the artifacts that leave the machine; a hole there is
  the leak this whole file exists to catch, so they are asserted by name and
  not merely covered by the walk.

Plus the file mode: the credential-bearing file is `0o600`.
"""

from __future__ import annotations

import argparse
import json
import stat
from pathlib import Path

import httpx

from conftest import make_mock_transport

from skill_from_docs import cmd_consolidate, cmd_fetch, cmd_probe


# The one file A8 allows to hold an unredacted URL, and the one key in it.
EXPECTED_HOLDER = Path("raw/source-map.json")
EXPECTED_HOLDER_KEY = "fetch_url"

# Named explicitly rather than left to the walk: these are the artifacts handed
# to skill-creator, so "the walk happened to cover them" is not good enough.
MUST_NOT_CONTAIN = (
    Path("docs.md"),
    Path("handoff.json"),
    Path("manifest.json"),
    Path("raw/spec.json"),
    Path("probes/a.json"),
    Path("probes/b.json"),
    Path("probes/c.json"),
)


SENTINEL_BASE = "SNTNL7f3c9a2b1d4e"

S_FETCH_QUERY = f"{SENTINEL_BASE}-fetch-query"
S_PROBE_QUERY = f"{SENTINEL_BASE}-probe-query"
S_USERINFO = f"{SENTINEL_BASE}-userinfo"
S_HEADER = f"{SENTINEL_BASE}-header"
S_JSON_BODY = f"{SENTINEL_BASE}-json-body"
S_NESTED_BODY = f"{SENTINEL_BASE}-nested-body"
S_FORM_BODY = f"{SENTINEL_BASE}-form-body"

ALL_SENTINELS = (
    S_FETCH_QUERY,
    S_PROBE_QUERY,
    S_USERINFO,
    S_HEADER,
    S_JSON_BODY,
    S_NESTED_BODY,
    S_FORM_BODY,
)


def _fetch_args(tmp_path: Path, source: str) -> argparse.Namespace:
    return argparse.Namespace(
        source=source,
        output_spec=None,
        output_source_map=None,
        no_resolve=True,
        user_agent=None,
        timeout=5.0,
        staleness_days=0,
        staleness_api_host=None,
        staleness_api_style=None,
        count_endpoints=False,
        allow_host=["api.example.com"],
        workspace=str(tmp_path),
        quiet=True,
    )


def _probe_args(tmp_path: Path, **overrides) -> argparse.Namespace:
    base = dict(
        url="https://api.example.com/v1/locations",
        method="GET",
        header=[],
        data=None,
        output=None,
        scope="ad-hoc",
        no_redact=False,
        redact_body_key=[],
        redact_body_pattern=[],
        allow_host=["api.example.com"],
        max_retries=0,
        follow_redirects=False,
        dry_run=False,
        timeout=2.0,
        workspace=str(tmp_path),
        quiet=True,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _consolidate_args(tmp_path: Path) -> argparse.Namespace:
    return argparse.Namespace(
        workspace=str(tmp_path),
        merge_probes=True,
        tag=[],
        narrative_dir=None,
        emit_handoff=True,
        sanitize=True,
        dry_run=False,
        quiet=True,
    )


def test_sentinel_credential_never_reaches_a_workspace_file(tmp_path: Path, fixtures_dir: Path):
    spec_text = (fixtures_dir / "tiny-openapi-3.json").read_text()

    # --- fetch: sentinel in the source URL's query string ------------------
    fetch_source = f"https://api.example.com/openapi.json?api_key={S_FETCH_QUERY}"
    fetch_transport = make_mock_transport(
        {
            fetch_source: httpx.Response(
                200,
                content=spec_text.encode(),
                headers={"Content-Type": "application/json"},
            ),
        }
    )
    rc = cmd_fetch.run(_fetch_args(tmp_path, fetch_source), transport=fetch_transport)
    assert rc == 0
    assert (tmp_path / "raw" / "spec.json").exists()
    assert (tmp_path / "raw" / "source-map.json").exists()

    # --- probe A: sentinel in URL query string ------------------------------
    probe_a_url = f"https://api.example.com/v1/locations?token={S_PROBE_QUERY}"

    def handler_a(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    rc = cmd_probe.run(
        _probe_args(tmp_path, url=probe_a_url, output=str(tmp_path / "probes" / "a.json")),
        transport=httpx.MockTransport(handler_a),
    )
    assert rc == 0

    # --- probe B: sentinel in headers + JSON request body (top-level and
    # nested key) ------------------------------------------------------------
    probe_b_url = "https://api.example.com/v1/locations/1"

    def handler_b(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    json_body = json.dumps(
        {
            "client_secret": S_JSON_BODY,
            "meta": {"password": S_NESTED_BODY},
            "region": "eu",
        }
    )
    rc = cmd_probe.run(
        _probe_args(
            tmp_path,
            url=probe_b_url,
            method="POST",
            header=[f"Authorization: Bearer {S_HEADER}"],
            data=json_body,
            output=str(tmp_path / "probes" / "b.json"),
        ),
        transport=httpx.MockTransport(handler_b),
    )
    assert rc == 0

    # --- probe C: sentinel in URL userinfo + form-encoded request body -----
    probe_c_url = f"https://{S_USERINFO}@api.example.com/v1/locations/2"

    def handler_c(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    form_body = f"grant_type=client_credentials&client_secret={S_FORM_BODY}&scope=read"
    rc = cmd_probe.run(
        _probe_args(
            tmp_path,
            url=probe_c_url,
            method="POST",
            data=form_body,
            output=str(tmp_path / "probes" / "c.json"),
        ),
        transport=httpx.MockTransport(handler_c),
    )
    assert rc == 0

    # --- consolidate ---------------------------------------------------------
    rc = cmd_consolidate.run(_consolidate_args(tmp_path))
    assert rc == 0
    assert (tmp_path / "docs.md").exists()
    assert (tmp_path / "handoff.json").exists()

    # --- walk every file the run produced ------------------------------------
    #
    # raw/spec.json needs no exemption: none of the sentinel positions above
    # land inside it, because the sentinels ride in the *fetch/probe request*
    # (URL, headers, request body) rather than in spec content itself — the
    # spec bytes written there come straight from the mocked response fixture,
    # which never contains a sentinel. It is in MUST_NOT_CONTAIN for that
    # reason.
    checked_files: list[Path] = []
    found: list[tuple[Path, str]] = []
    for path in sorted(tmp_path.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(tmp_path)
        checked_files.append(rel)
        text = path.read_text(encoding="utf-8", errors="replace")
        for sentinel in ALL_SENTINELS:
            if sentinel in text:
                found.append((rel, sentinel))

    # Sanity: the run actually produced the files this test means to check.
    relative = set(checked_files)
    for expected in (EXPECTED_HOLDER, *MUST_NOT_CONTAIN):
        assert expected in relative, f"expected artifact missing: {expected}"
    assert len(list((tmp_path / "probes").glob("*.json"))) == 3

    # 1. The named artifacts — the ones handed to skill-creator — are clean.
    #    Asserted by name so that renaming or dropping one of them fails here
    #    rather than silently shrinking the guarantee.
    named_leaks = [(str(p), s) for p, s in found if p in MUST_NOT_CONTAIN]
    assert not named_leaks, f"sentinel leaked into a handoff artifact: {named_leaks}"

    # 2. Nothing outside the one permitted holder carries a sentinel either,
    #    including files this test does not know the name of.
    stray_leaks = [(str(p), s) for p, s in found if p != EXPECTED_HOLDER]
    assert not stray_leaks, f"sentinel leaked into workspace files: {stray_leaks}"

    # 3. The permitted holder carries it at exactly one key — `fetch_url`, the
    #    URL A8 records so the audit trail stays re-fetchable — and nowhere
    #    else in the file. `spec_url`, which is the value everything downstream
    #    copies, is the redacted spelling.
    source_map = json.loads((tmp_path / EXPECTED_HOLDER).read_text())
    assert source_map[EXPECTED_HOLDER_KEY] == fetch_source
    assert S_FETCH_QUERY in source_map[EXPECTED_HOLDER_KEY]
    assert S_FETCH_QUERY not in source_map["spec_url"]

    rest = {k: v for k, v in source_map.items() if k != EXPECTED_HOLDER_KEY}
    rest_leaks = [s for s in ALL_SENTINELS if s in json.dumps(rest)]
    assert not rest_leaks, f"sentinel outside {EXPECTED_HOLDER_KEY}: {rest_leaks}"

    # 4. And it is not world-readable. This file did not hold a credential
    #    before A8; the mode is what makes putting one there defensible.
    mode = stat.S_IMODE((tmp_path / EXPECTED_HOLDER).stat().st_mode)
    assert mode == 0o600, f"{EXPECTED_HOLDER} is {oct(mode)}, want 0o600"
