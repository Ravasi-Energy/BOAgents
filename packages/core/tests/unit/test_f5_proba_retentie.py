"""@requirement HIR-REQ-040 — regresii proba retentie cu eliminare
efectiva (H-R-10, SC00, revizia runnerului).

Preflight complet (containment/symlink/suprapunere/repo strain/marcaj
propriu/hash adaptor/retentie/runde), spy cu stare persistenta care
emuleaza semantica restic (backup/forget --prune/ls/cat/restore), si
lantul REAL restic 0.19.1 + adaptorul de productie vendoruit pe Mac.
PROBA PE DISC — partea S3 ramane NEEXECUTAT/HOLD indiferent de verdict.
"""
import hashlib
import json
import os
import shutil
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", ".."))
TOOLS = os.path.join(REPO_ROOT, "tools", "val4")
FIXTURA = os.path.join(TOOLS, "fixtura_hire")
ADAPTOR_REAL = os.path.join(FIXTURA, "backups", "restic-backup.sh")
sys.path.insert(0, TOOLS)

import proba_retentie_restic as ret_r  # noqa: E402

# restic real: pe Mac exista binarul pinat 0.19.1; in CI lipseste →
# pozitivul real se sare onest, restul matricei ruleaza cu spy.
RESTIC_MAC = os.path.realpath(os.path.join(
    REPO_ROOT, "..", "instrumente-f5", "restic", "restic"))
RESTIC_REAL = RESTIC_MAC if os.path.isfile(RESTIC_MAC) \
    else shutil.which("restic")


def _sha(cale):
    return hashlib.sha256(Path(cale).read_bytes()).hexdigest()


def _argv(zona, repo=None, extra=()):
    a = ["--zona", zona,
         "--repository", repo or os.path.join(zona, "repo-test"),
         "--password-file", os.path.abspath("pf"),
         "--adapter", os.path.abspath("adapter.sh"),
         "--restic-bin", os.path.abspath("restic-spy")]
    if os.path.isfile("adapter.sh"):
        a += ["--adapter-sha256", _sha("adapter.sh")]
    return a + list(extra)


def _fixturi(tmp_path, restic_continut=None, adapter_continut=None):
    pf = tmp_path / "pf"
    pf.write_text("parola-test")
    spy = tmp_path / "restic-spy"
    spy.write_text(restic_continut or "#!/bin/sh\nexit 0\n")
    spy.chmod(0o755)
    ad = tmp_path / "adapter.sh"
    ad.write_text(adapter_continut or "#!/bin/sh\nexit 0\n")
    ad.chmod(0o755)


def _adapter_spy(zona):
    """Adaptor de test: acelasi contract ca productia —
    backup apoi forget --keep-* --prune prin $RESTIC_BIN."""
    return textwrap.dedent("""\
        #!/bin/sh
        "$RESTIC_BIN" backup $BACKUP_SOURCES
        "$RESTIC_BIN" forget --keep-daily $BACKUP_KEEP_DAILY \\
            --keep-weekly $BACKUP_KEEP_WEEKLY --prune
    """)


def _restic_spy(zona):
    """restic instrumentat cu stare persistenta: snapshoturi in acelasi
    grup (aceeasi zi), forget --keep-daily 1 elimina toate minus cel mai
    nou si marcheaza bloburile lor drept sterse, cat blob verifica.
    Emuleaza doar subcomenzile folosite de proba + adaptor."""
    stare = os.path.join(str(zona), ".stare-spy")
    return textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import json, os, shutil, sys
        STARE = {stare!r}
        a = sys.argv[1:]
        repo = ""
        if "-r" in a:
            j = a.index("-r")
            repo = a[j+1]
            a = a[:j] + a[j+2:]
        repo = repo or os.environ.get("RESTIC_REPOSITORY", "")
        os.makedirs(STARE, exist_ok=True)
        p = os.path.join(STARE, "stare.json")
        st = json.load(open(p)) if os.path.exists(p) else \\
            {{"seq": 0, "snaps": {{}}, "stersi": []}}
        def salveaza():
            json.dump(st, open(p, "w"))
        cmd = a[0] if a else ""
        if cmd == "init":
            if repo:
                os.makedirs(repo, exist_ok=True)
                open(os.path.join(repo, "config"), "w").write("{{}}")
            print("created restic repository")
            sys.exit(0)
        if cmd == "backup":
            st["seq"] += 1
            sid = "snap-%04d" % st["seq"]
            src = a[-1]
            fisiere = {{}}
            for f in os.listdir(src):
                fp = os.path.join(src, f)
                if os.path.isfile(fp):
                    bid = "blob-%s-%s" % (sid, f.replace(" ", "_"))
                    dst = os.path.join(STARE, bid)
                    shutil.copyfile(fp, dst)
                    fisiere[f] = bid
            st["snaps"][sid] = {{
                "id": sid,
                "time": "2026-10-15T%02d:00:00Z" % st["seq"],
                "paths": [os.path.realpath(src)],
                "hostname": "spymac",
                "files": fisiere}}
            salveaza()
            print("snapshot %s saved" % sid)
            sys.exit(0)
        if cmd == "forget":
            kd = kw = 3650
            for j, x in enumerate(a):
                if x == "--keep-daily":
                    kd = int(a[j+1])
                if x == "--keep-weekly":
                    kw = int(a[j+1])
            politica = min(kd, kw) if min(kd, kw) > 0 else max(kd, kw)
            vii = [s for s in st["snaps"] if s not in st["stersi"]]
            vii.sort(key=lambda s: st["snaps"][s]["time"])
            taiati = vii[:-politica] if politica < len(vii) else []
            for s in taiati:
                st["stersi"].append(s)
            salveaza()
            if "--prune" in a and taiati:
                print("remove %d snapshots" % len(taiati))
                print("freed 1.0 KiB")
            sys.exit(0)
        if cmd == "snapshots":
            print(json.dumps([
                {{"id": s, "short_id": s[:8],
                  "time": st["snaps"][s]["time"],
                  "hostname": "spymac",
                  "paths": st["snaps"][s]["paths"]}}
                for s in st["snaps"] if s not in st["stersi"]]))
            sys.exit(0)
        if cmd == "cat" and len(a) > 2 and a[1] == "tree":
            sid = a[2].split(":")[0]
            snap = st["snaps"].get(sid)
            if not snap or sid in st["stersi"]:
                sys.exit(1)
            print(json.dumps({{"nodes": [
                {{"name": f, "type": "file", "content": [bid]}}
                for f, bid in snap["files"].items()]}}))
            sys.exit(0)
        if cmd == "cat" and len(a) > 2 and a[1] == "blob":
            bid = a[2]
            # blobul supravietuieste daca vreun snapshot VIU il tine
            for s in st["snaps"]:
                if s not in st["stersi"] \\
                        and bid in st["snaps"][s]["files"].values():
                    sys.exit(0)
            sys.exit(1)
        if cmd == "restore":
            sid = a[1]
            tgt = a[-1]
            snap = st["snaps"].get(sid)
            if not snap or sid in st["stersi"]:
                sys.exit(1)
            os.makedirs(tgt, exist_ok=True)
            for f, bid in snap["files"].items():
                shutil.copyfile(os.path.join(STARE, bid),
                                os.path.join(tgt, f))
            sys.exit(0)
        if cmd == "check":
            sys.exit(0)
        sys.exit(0)
    """)


# --- refuzuri fara efect (zero fisiere create inainte de refuz) -----

def test_ret_fara_zona_refuza(tmp_path):
    with pytest.raises(SystemExit):
        ret_r.main([])


def test_ret_password_lipsa_refuza(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(tmp_path)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and out["verdict"] == "REFUZ"
    assert out["tip"] == "PROBĂ RETENȚIE PE DISC"
    assert out["s3"] == "NEEXECUTAT/HOLD"


def test_ret_restic_bin_lipsa_refuza(tmp_path, capsys, monkeypatch):
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(tmp_path / "zona"), extra=[
        "--restic-bin", "restic-care-nu-exista-xyz"]))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "restic" in out["motiv"]
    assert not os.path.exists(str(tmp_path / "zona"))


def test_ret_runde_invalide_refuza(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    for k in ("0", "1", "11", "abc", "-2", "2.5", "٣"):
        cod = ret_r.main(_argv(str(zona), extra=["--runde", k]))
        assert cod == 2, k
        assert not os.path.exists(str(zona)), k


def test_ret_retentie_invalida_refuza(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    for k in ("abc", "9999", "-1", "1.5"):
        cod = ret_r.main(_argv(str(zona), extra=["--keep-daily", k]))
        assert cod == 2, k


def test_ret_retentie_zero_zero_refuza(tmp_path, capsys, monkeypatch):
    """0/0 ar sterge si snapshotul pastrat — refuz, nu proba oarba."""
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona), extra=[
        "--keep-daily", "0", "--keep-weekly", "0"]))
    assert cod == 2
    assert not os.path.exists(str(zona))


def test_ret_repo_sibling_refuzat(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona), repo=str(tmp_path / "repo-strain")))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "afara zonei" in out["motiv"]
    assert not os.path.exists(str(zona))


def test_ret_repo_symlink_refuzat(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in-afara").mkdir()
    (tmp_path / "zona-link").symlink_to(tmp_path / "in-afara")
    cod = ret_r.main(_argv(str(zona), repo=str(tmp_path / "zona-link"
                                               / "repo")))
    assert cod == 2


def test_ret_repo_strain_refuzat(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    (zona / "repo-test").mkdir(parents=True)
    (zona / "repo-test" / "fisier-strain.txt").write_text("x")
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "strain" in out["motiv"]
    assert (zona / "repo-test" / "fisier-strain.txt").exists()


def test_ret_repo_al_probei_disc_refuzat(tmp_path, capsys, monkeypatch):
    """Repo-ul creat de proba_disc (marcaj .proba-disc-f5) NU e al
    probei de retentie — forget --prune i-ar sterge snapshoturile."""
    zona = tmp_path / "zona"
    (zona / "repo-test").mkdir(parents=True)
    (zona / "repo-test" / "config").write_text("{}")
    (zona / "repo-test" / ".proba-disc-f5").write_text("proba-disc-f5\n")
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "strain" in out["motiv"]


def test_ret_repo_dedicat_refolosit(tmp_path, capsys, monkeypatch):
    """config + marcajul propriu (.proba-retentie-f5) → dedicat."""
    zona = tmp_path / "zona"
    (zona / "repo-test").mkdir(parents=True)
    (zona / "repo-test" / "config").write_text("{}")
    (zona / "repo-test" / ".proba-retentie-f5").write_text(
        "proba-retentie-f5\n")
    _fixturi(tmp_path,
             restic_continut=_restic_spy(str(zona)),
             adapter_continut=_adapter_spy(str(zona)))
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 0 and out["verdict"] == "PASS", out


def test_ret_suprapuneri_refuzate(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    acelasi = str(zona / "acelasi")
    cod = ret_r.main(_argv(str(zona), repo=acelasi, extra=[
        "--sources-dir", acelasi]))
    assert cod == 2


def test_ret_director_pregatit_refuzat(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    (zona / "surse").mkdir(parents=True)
    (zona / "surse" / "deja-aici.txt").write_text("x")
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert ret_r.main(_argv(str(zona))) == 2


def test_ret_adapter_hash_gresit_refuza(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona), extra=[
        "--adapter-sha256", "0" * 64]))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "adaptor" in out["motiv"]


def test_ret_cale_cu_newline_refuzata(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona), repo=str(zona / "repo\nstrain")))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "control" in out["motiv"]


def test_ret_cache_symlink_refuzat(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    zona.mkdir(parents=True)
    (zona / ".cache-restic").symlink_to(tmp_path / "in-afara")
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert ret_r.main(_argv(str(zona))) == 2


# --- pozitive si esecuri oneste cu spy -----------------------------

def test_ret_pozitiv_spy_eliminare(tmp_path, capsys, monkeypatch):
    """Lantul complet cu procese instrumentate: 3 runde in acelasi
    grup, politica keep-daily=1, 2 snapshoturi eliminate, bloburile
    unice sterse prin prune, restore+hash pe cel pastrat."""
    zona = tmp_path / "zona"
    _fixturi(tmp_path,
             restic_continut=_restic_spy(str(zona)),
             adapter_continut=_adapter_spy(str(zona)))
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 0 and out["verdict"] == "PASS", out
    assert all(p["ok"] for p in out["pasi"])
    # grup inainte/dupa + eliminare efectiva in raport
    assert len(out["inainte"]["snapshoturi"]) == 2
    assert len(out["dupa"]["snapshoturi"]) == 1
    assert out["dupa"]["snapshoturi"] == [out["pastrat"]]
    assert len(out["eliminate"]) == 2
    for fis, inf in out["eliminate"].items():
        assert fis.startswith("unic-runda-")
        assert inf["eliminat_prin_prune"] is True
        assert inf["bloburi"]
    # marcajul propriu + raportul cu etichetele obligatorii
    assert Path(zona, "repo-test", ".proba-retentie-f5").exists()
    rap = Path(zona, "raport-retentie", "PROBA-RETENTIE.json")
    assert rap.exists()
    continut = rap.read_text()
    assert '"PROBĂ RETENȚIE PE DISC"' in continut
    assert '"NEEXECUTAT/HOLD"' in continut


def test_ret_fara_forget_nu_pass(tmp_path, capsys, monkeypatch):
    """Adaptor care NU face forget/prune (sau prune fara efect):
    snapshoturile raman → FAIL, nu PASS. Nu promovam exit0 in
    eliminare — limita exacta reprobata pe 15.10."""
    zona = tmp_path / "zona"
    adapter_fara_retentie = "#!/bin/sh\n" \
        '"$RESTIC_BIN" backup $BACKUP_SOURCES\n'
    _fixturi(tmp_path,
             restic_continut=_restic_spy(str(zona)),
             adapter_continut=adapter_fara_retentie)
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"
    esuate = [p for p in out["pasi"] if not p["ok"]]
    assert any("eliminate" in p["pas"] for p in esuate)


def test_ret_env_mostenit_nu_trece(tmp_path, capsys, monkeypatch):
    """Env-ul procesului parinte nu se revarsa in adaptor/restic."""
    monkeypatch.setenv("BACKUP_EXTRA_ARGS", "--evil-env")
    monkeypatch.setenv("BACKUP_SOURCES", "/var/lib/docker/volumes/prod")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cheie-secret")
    zona = tmp_path / "zona"
    spy_log = tmp_path / "spy.log"
    adapter = textwrap.dedent(f"""\
        #!/bin/sh
        echo "X=$BACKUP_EXTRA_ARGS|$AWS_SECRET_ACCESS_KEY" >> "{spy_log}"
        "$RESTIC_BIN" backup $BACKUP_SOURCES
        "$RESTIC_BIN" forget --keep-daily $BACKUP_KEEP_DAILY \\
            --keep-weekly $BACKUP_KEEP_WEEKLY --prune
    """)
    _fixturi(tmp_path,
             restic_continut=_restic_spy(str(zona)),
             adapter_continut=adapter)
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    log = spy_log.read_text() if spy_log.exists() else ""
    assert cod == 0, out
    assert "--evil-env" not in log and "cheie-secret" not in log


def test_ret_backup_esuat_nu_pass(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path,
             restic_continut="#!/bin/sh\nexit 1\n",
             adapter_continut=_adapter_spy(str(zona)))
    monkeypatch.chdir(tmp_path)
    cod = ret_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"


# --- proba reala Mac: restic pinat + adaptor de productie -----------

@pytest.mark.skipif(
    RESTIC_REAL is None or not os.path.isfile(ADAPTOR_REAL),
    reason="restic real sau adaptorul vendoruit lipsesc — probă Mac")
def test_ret_pozitiv_restic_real_adaptor_produs(tmp_path, capsys):
    """Lantul REAL cap-coada pe Mac: restic 0.19.1 + adaptorul de
    productie vendoruit. 3 snapshoturi reale in acelasi grup,
    keep-daily=1: 2 eliminate + prune care sterge bloburile nepartajate
    (cat blob esueaza dupa, reusea inainte), restic check, restore cu
    hashuri. Pinul sha256 al adaptorului verificat implicit."""
    zona = tmp_path / "zona"
    pf = tmp_path / "pf"
    pf.write_text("parola-test-123")
    cod = ret_r.main([
        "--zona", str(zona),
        "--repository", str(zona / "repo-test"),
        "--password-file", str(pf),
        "--adapter", ADAPTOR_REAL,
        "--restic-bin", RESTIC_REAL,
        "--keep-daily", "1", "--keep-weekly", "0"])
    out = json.loads(capsys.readouterr().out)
    assert cod == 0 and out["verdict"] == "PASS", out
    assert all(p["ok"] for p in out["pasi"])
    assert Path(zona, "repo-test", "config").exists()
    assert Path(zona, "repo-test", ".proba-retentie-f5").exists()
    # eliminare efectiva reala, nu doar listare
    assert len(out["eliminate"]) == 2
    assert all(i["eliminat_prin_prune"] for i in out["eliminate"].values())
    # restaurate exacte, incl. fisierul cu spatii
    restaurate = list(Path(zona, "restore").rglob("*spatii*"))
    assert len(restaurate) == 1


@pytest.mark.skipif(
    not os.path.isfile(os.path.join(
        os.path.realpath(os.path.join(
            REPO_ROOT, "..", "..", "BO-A03", "Hire-val4-01")),
        "backups", "restic-backup.sh")),
    reason="checkoutul Hire real exista numai pe Mac — drift-hash local")
def test_ret_adaptor_drift_vs_checkout_real():
    a = Path(ADAPTOR_REAL).read_bytes()
    b = Path(os.path.realpath(os.path.join(
        REPO_ROOT, "..", "..", "BO-A03", "Hire-val4-01")),
        "backups", "restic-backup.sh").read_bytes()
    assert a == b, "drift restic-backup.sh"
