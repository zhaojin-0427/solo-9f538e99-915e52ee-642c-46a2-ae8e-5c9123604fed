"""Correction acceptance: both revisions are observed, atomic, and recorded.

Covers the reported defects:

* two corrections based on the same old instance revision must not both win;
* a package revised between parsing and persistence must reject the write;
* the package revision actually used to parse the stored facts is recorded,
  and legacy revisions without that basis stay readable with ``None``.
"""
from __future__ import annotations

import sqlite3

from conftest import check, create_instance, create_package


def revise(client, instance_id, xml, inst_rev, pkg_rev):
    return client.post(
        f"/instances/{instance_id}/revisions",
        headers={
            "Expected-Instance-Revision": str(inst_rev),
            "Expected-Package-Revision": str(pkg_rev),
        },
        files={"instance": ("i.xml", xml, "application/xml")},
    )


def test_second_correction_from_same_base_is_rejected(client, samples):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["infeasible"])
    iid = inst["instance_id"]

    # A report exists before the corrections; a rejected correction must not
    # touch it.
    first_report = check(client, iid, 1, 1).json()["report"]

    # Operator A and operator B both saw instance rev 1 / package rev 1.
    a = revise(client, iid, samples["feasible"], 1, 1)
    assert a.status_code == 200, a.text
    assert a.json()["latest_revision"] == 2

    b = revise(client, iid, samples["rounding"], 1, 1)
    assert b.status_code == 412, b.text
    assert b.json()["error"]["code"] == "stale_revision"

    # No revision was appended and the stored facts are still A's.
    revisions = client.get(f"/instances/{iid}/revisions").json()
    assert [r["revision"] for r in revisions] == [1, 2]
    latest = client.get(f"/instances/{iid}").json()
    assert latest["latest_revision"] == 2
    facts = {f["concept"]: f["value"] for f in latest["latest"]["facts"]}
    assert facts["acct:CurrentAssets"] == 200  # feasible sample, not rounding's 201

    # Reports are untouched by the rejected correction.
    listing = client.get(f"/instances/{iid}/reports").json()
    assert [r["report_id"] for r in listing["history"]] == [first_report["report_id"]]
    assert listing["history"][0]["superseded"] is False


def test_correction_requires_both_headers(client, samples):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["feasible"])

    r = client.post(
        f"/instances/{inst['instance_id']}/revisions",
        headers={"Expected-Package-Revision": "1"},
        files={"instance": ("i.xml", samples["feasible"], "application/xml")},
    )
    assert r.status_code == 422  # Expected-Instance-Revision is mandatory
    assert client.get(f"/instances/{inst['instance_id']}").json()["latest_revision"] == 1


def test_correction_rejected_when_package_moved_since_parse(client, samples, monkeypatch):
    """The package advancing *while the correction is parsed* must 412.

    ``parse_instance`` is wrapped so that it revises the package after
    reading the observed revision but before the write transaction starts —
    exactly the window in which facts from the old package used to be stored.
    """
    from app import storage

    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["infeasible"])
    iid = inst["instance_id"]
    report = check(client, iid, 1, 1).json()["report"]

    real_parse = storage.parse_instance

    def racing_parse(data, package_info):
        parsed = real_parse(data, package_info)
        # Bump the package behind the in-flight correction.
        r = client.post(
            f"/packages/{pkg['package_id']}/revisions",
            headers={"Expected-Revision": "1"},
            files={
                "xsd": ("acct.xsd", samples["xsd"], "application/xml"),
                "calculation": ("c.xml", samples["calc"], "application/xml"),
            },
        )
        assert r.status_code == 200, r.text
        return parsed

    monkeypatch.setattr(storage, "parse_instance", racing_parse)

    r = revise(client, iid, samples["feasible"], 1, 1)
    assert r.status_code == 412, r.text
    assert "package" in r.json()["error"]["message"]

    # Neither an instance revision nor a report changed.
    assert client.get(f"/instances/{iid}").json()["latest_revision"] == 1
    revisions = client.get(f"/instances/{iid}/revisions").json()
    assert len(revisions) == 1
    listing = client.get(f"/instances/{iid}/reports").json()
    assert [x["report_id"] for x in listing["history"]] == [report["report_id"]]

    # The observed package revision is genuinely current again (2), and a
    # correction against it is accepted.  Stop racing, or the accepted
    # correction would bump the package a second time.
    monkeypatch.setattr(storage, "parse_instance", real_parse)
    ok = revise(client, iid, samples["feasible"], 1, 2)
    assert ok.status_code == 200, ok.text


def test_correction_records_package_revision_used_for_parsing(client, samples):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["feasible"])
    iid = inst["instance_id"]
    assert inst["latest"]["package_revision"] == 1

    # Package rev 2 (same taxonomy content for this test).
    r = client.post(
        f"/packages/{pkg['package_id']}/revisions",
        headers={"Expected-Revision": "1"},
        files={
            "xsd": ("acct.xsd", samples["xsd"], "application/xml"),
            "calculation": ("c.xml", samples["calc"], "application/xml"),
        },
    )
    assert r.status_code == 200

    # Stale package view is rejected even though the instance revision is right.
    stale = revise(client, iid, samples["rounding"], 1, 1)
    assert stale.status_code == 412
    assert "package" in stale.json()["error"]["message"]

    ok = revise(client, iid, samples["rounding"], 1, 2)
    assert ok.status_code == 200, ok.text
    assert ok.json()["latest"]["package_revision"] == 2
    revisions = client.get(f"/instances/{iid}/revisions").json()
    assert [x["package_revision"] for x in revisions] == [1, 2]

    # A report records both the linkbase revision checked and the revision
    # that produced the stored facts.
    report = check(client, iid, 2, 2).json()["report"]
    assert report["package_revision"] == 2
    assert report["instance_package_revision"] == 2
    assert report["detail"]["instance_package_revision"] == 2
    assert report["detail"]["package_basis_differs"] is False
    assert report["detail"]["package_basis_unknown"] is False

    # Late report checking old facts against the new linkbase keeps both bases.
    late = check(client, iid, 1, 2, late=True).json()["report"]
    assert late["late"] is True
    assert late["instance_revision"] == 1
    assert late["instance_package_revision"] == 1
    assert late["package_revision"] == 2


def test_correction_never_silently_falls_back_to_an_older_package(client, samples):
    """A correction is parsed with the observed package revision or not at all.

    If the observed package revision dropped a concept the payload still uses,
    the correction is rejected (422) and neither a revision nor a report is
    written.  Old facts remain readable and the report shows the package
    revision that actually produced them instead of implying the new one did.
    """
    # Rev 2 taxonomy drops Inventory from the XSD and its calculation arcs.
    xsd2 = samples["xsd"].replace(
        b'  <xs:element name="Inventory" id="acct_Inventory" type="xs:decimal" abstract="false"/>\n',
        b"",
    )
    assert xsd2 != samples["xsd"]
    calc2 = samples["calc"].replace(
        b"""    <link:loc xlink:type="locator" xlink:label="loc_Inventory"
              xlink:href="acct.xsd#Inventory"/>
""",
        b"",
    )
    calc2 = calc2.replace(
        b"""    <calc:calculationArc xlink:type="arc"
                         xlink:from="loc_CurrentAssets" xlink:to="loc_Inventory"
                         xlink:arcrole="http://www.xbrl.org/2003/arcrole/summation-item"
                         weight="1.0"/>
""",
        b"",
    )
    assert calc2 != samples["calc"]

    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["feasible"])
    iid = inst["instance_id"]
    first_report = check(client, iid, 1, 1).json()["report"]

    r = client.post(
        f"/packages/{pkg['package_id']}/revisions",
        headers={"Expected-Revision": "1"},
        files={
            "xsd": ("acct.xsd", xsd2, "application/xml"),
            "calculation": ("c.xml", calc2, "application/xml"),
        },
    )
    assert r.status_code == 200, r.text

    # The feasible payload still carries acct:Inventory, which rev 2 no
    # longer declares: it must be a 422, not a quiet parse against rev 1.
    rejected = revise(client, iid, samples["feasible"], 1, 2)
    assert rejected.status_code == 422, rejected.text
    assert "Inventory" in rejected.json()["error"]["message"]
    assert client.get(f"/instances/{iid}").json()["latest_revision"] == 1
    revisions = client.get(f"/instances/{iid}/revisions").json()
    assert [x["revision"] for x in revisions] == [1]
    listing = client.get(f"/instances/{iid}/reports").json()
    assert [x["report_id"] for x in listing["history"]] == [first_report["report_id"]]

    # A late check of the old facts against the new linkbase is allowed and
    # records both bases separately: facts came from rev 1, links from rev 2.
    late = check(client, iid, 1, 2, late=True).json()["report"]
    assert late["package_revision"] == 2
    assert late["instance_package_revision"] == 1
    assert late["detail"]["package_basis_differs"] is True
    assert late["detail"]["package_basis_unknown"] is False
    # Rev 2 drops Inventory from the CurrentAssets sum, so the old facts no
    # longer balance under the new links — the report says so explicitly.
    assert late["status"] == "infeasible"


def test_correction_rejects_ahead_instance_revision(client, samples):
    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["infeasible"])
    iid = inst["instance_id"]

    ahead = revise(client, iid, samples["feasible"], 99, 1)
    assert ahead.status_code == 412
    assert "instance" in ahead.json()["error"]["message"]
    assert client.get(f"/instances/{iid}").json()["latest_revision"] == 1
    assert len(client.get(f"/instances/{iid}/revisions").json()) == 1


def test_check_rejected_if_package_moves_while_solver_runs(client, samples, monkeypatch):
    """A current check must not persist a report against a state that moved."""
    from app import storage

    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["feasible"])
    iid = inst["instance_id"]

    real_check = storage.check_facts

    def moving_check(facts, equations):
        out = real_check(facts, equations)
        r = client.post(
            f"/packages/{pkg['package_id']}/revisions",
            headers={"Expected-Revision": "1"},
            files={
                "xsd": ("acct.xsd", samples["xsd"], "application/xml"),
                "calculation": ("c.xml", samples["calc"], "application/xml"),
            },
        )
        assert r.status_code == 200, r.text
        return out

    monkeypatch.setattr(storage, "check_facts", moving_check)
    r = check(client, iid, 1, 1)
    assert r.status_code == 412, r.text
    assert client.get(f"/instances/{iid}/reports").json()["history"] == []

    # Against the now-current package revision the check succeeds.
    monkeypatch.setattr(storage, "check_facts", real_check)
    ok = check(client, iid, 1, 2)
    assert ok.status_code == 200, ok.text
    assert len(client.get(f"/instances/{iid}/reports").json()["history"]) == 1


def test_concurrent_corrections_from_same_base_accept_exactly_one(client, samples):
    """Both operators submit at the same time from instance rev 1.

    A barrier inside parsing makes both requests read the old revision before
    either writes; exactly one must commit and the other must get 412 without
    appending a revision built on the same base.
    """
    import threading

    from app import storage

    pkg = create_package(client, samples)
    inst = create_instance(client, pkg["package_id"], samples["infeasible"])
    iid = inst["instance_id"]

    real_parse = storage.parse_instance
    barrier = threading.Barrier(2)
    lock = threading.Lock()
    calls = []

    def synced_parse(data, package_info):
        with lock:
            calls.append(1)
            first_two = len(calls) <= 2
        if first_two:
            barrier.wait(timeout=10)
        return real_parse(data, package_info)

    storage.parse_instance = synced_parse
    results = []

    def submit(xml):
        results.append(revise(client, iid, xml, 1, 1))

    try:
        threads = [
            threading.Thread(target=submit, args=(samples["feasible"],)),
            threading.Thread(target=submit, args=(samples["rounding"],)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        storage.parse_instance = real_parse

    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 412], [r.text for r in results]
    assert client.get(f"/instances/{iid}").json()["latest_revision"] == 2
    assert len(client.get(f"/instances/{iid}/revisions").json()) == 2


def test_legacy_revisions_without_basis_are_readable_and_not_guessed(tmp_path, samples):
    """A pre-existing database (no basis columns) migrates without inventing
    a package revision for its historical rows."""
    old = tmp_path / "legacy" / "xrecon.db"
    old.parent.mkdir(parents=True)
    conn = sqlite3.connect(old)
    conn.executescript(
        """
        CREATE TABLE packages (
            id TEXT PRIMARY KEY, name TEXT NOT NULL,
            latest_rev INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE package_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            package_id TEXT NOT NULL, revision INTEGER NOT NULL,
            xsd_raw BLOB NOT NULL, calc_raw BLOB NOT NULL,
            target_ns TEXT NOT NULL, prefix TEXT NOT NULL,
            concepts_json TEXT NOT NULL, equations_json TEXT NOT NULL,
            created_at TEXT NOT NULL, UNIQUE(package_id, revision));
        CREATE TABLE instances (
            id TEXT PRIMARY KEY, package_id TEXT NOT NULL,
            latest_rev INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE instance_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            instance_id TEXT NOT NULL, revision INTEGER NOT NULL,
            instance_raw BLOB NOT NULL, entity TEXT NOT NULL,
            period TEXT NOT NULL, currency TEXT NOT NULL,
            facts_json TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(instance_id, revision));
        CREATE TABLE reports (
            id TEXT PRIMARY KEY, instance_id TEXT NOT NULL,
            package_id TEXT NOT NULL, instance_revision INTEGER NOT NULL,
            package_revision INTEGER NOT NULL, status TEXT NOT NULL,
            is_correction INTEGER NOT NULL, superseded INTEGER NOT NULL DEFAULT 0,
            late INTEGER NOT NULL DEFAULT 0, witness_json TEXT,
            conflicts_json TEXT, missing_json TEXT, detail_json TEXT NOT NULL,
            instance_snapshot BLOB NOT NULL, xsd_snapshot BLOB NOT NULL,
            calc_snapshot BLOB NOT NULL, created_at TEXT NOT NULL);
        """
    )
    import json

    from app.instance import parse_instance
    from app.taxonomy import parse_package

    pkg_info = parse_package(samples["xsd"], samples["calc"])
    inst_info = parse_instance(samples["feasible"], pkg_info)
    conn.execute(
        "INSERT INTO packages VALUES ('pkg_old','old',1,'t0','t0')")
    conn.execute(
        "INSERT INTO package_revisions(package_id, revision, xsd_raw, calc_raw, "
        "target_ns, prefix, concepts_json, equations_json, created_at) "
        "VALUES ('pkg_old',1,?,?,?,?,?,?, 't0')",
        (samples["xsd"], samples["calc"], pkg_info["target_ns"], pkg_info["prefix"],
         json.dumps(pkg_info["concepts"]), json.dumps(pkg_info["equations"])))
    conn.execute(
        "INSERT INTO instances VALUES ('inst_old','pkg_old',1,'t0','t0')")
    conn.execute(
        "INSERT INTO instance_revisions(instance_id, revision, instance_raw, entity, "
        "period, currency, facts_json, created_at) "
        "VALUES ('inst_old',1,?,?,?,?,?,'t0')",
        (samples["feasible"], inst_info["entity"], inst_info["period"],
         inst_info["currency"], json.dumps(inst_info["facts"])))
    conn.execute(
        "INSERT INTO reports(id, instance_id, package_id, instance_revision, "
        "package_revision, status, is_correction, superseded, late, detail_json, "
        "instance_snapshot, xsd_snapshot, calc_snapshot, created_at) "
        "VALUES ('rpt_old','inst_old','pkg_old',1,1,'feasible',0,0,0,'{}',?,?,?,'t0')",
        (samples["feasible"], samples["xsd"], samples["calc"]))
    conn.commit()
    conn.close()

    import os
    from pathlib import Path
    from fastapi.testclient import TestClient

    os.environ["XRECON_DATA_DIR"] = str(old.parent)
    os.environ["XRECON_DB_PATH"] = str(old)
    from app import config, db
    config.DATA_DIR = Path(old.parent)
    config.DB_PATH = Path(old)
    db.reset_conn()
    try:
        from app.main import app
        with TestClient(app) as client:
            inst = client.get("/instances/inst_old")
            assert inst.status_code == 200
            assert inst.json()["latest"]["package_revision"] is None
            revisions = client.get("/instances/inst_old/revisions").json()
            assert revisions[0]["package_revision"] is None

            report = client.get("/reports/rpt_old")
            assert report.status_code == 200
            assert report.json()["instance_package_revision"] is None
            # Raw snapshot still served.
            assert client.get(
                "/reports/rpt_old/snapshots/instance"
            ).content == samples["feasible"]

            # A check of a legacy revision still works, and its report says
            # the factual basis is unknown rather than borrowing package rev 1.
            fresh = client.post(
                "/instances/inst_old/checks",
                headers={"Expected-Instance-Revision": "1",
                         "Expected-Package-Revision": "1"},
            )
            assert fresh.status_code == 200, fresh.text
            body = fresh.json()["report"]
            assert body["instance_package_revision"] is None
            assert body["package_revision"] == 1
            assert body["detail"]["package_basis_unknown"] is True
    finally:
        db.reset_conn()
        os.environ.pop("XRECON_DATA_DIR", None)
        os.environ.pop("XRECON_DB_PATH", None)
