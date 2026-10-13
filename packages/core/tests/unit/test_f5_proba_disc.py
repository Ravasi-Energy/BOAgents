"""@requirement HIR-REQ-040 — regresii proba pe disc restic (H-R-10, SC00).

Preflight complet (containment/symlink/suprapunere/repo strain/marcaj
dedicat/hash adaptor/retentie), lantul real restic local + adaptorul de
productie pe Mac, transport de procese instrumentat in CI. PROBA PE DISC
— partea S3 ramane NEEXECUTAT/HOLD si nu devine PASS prin aceasta proba.
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

import proba_disc_restic as disc_r  # noqa: E402

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


def _spy_cu_surat(zona, log=None):
    surse_r = os.path.realpath(os.path.join(zona, "surse"))
    linii = ["#!/bin/sh"]
    if log:
        linii.append(f'echo "$@" >> "{log}"')
    linii += [
        'case "$*" in',
        '  *"snapshots"*) echo \'[{"id":"snap-disc",'
        f'"time":"2026-10-13T09:00:00Z","paths":["{surse_r}"]}}]\';;',
        '  *"restore"*) eval "tgt=\\$$#"; mkdir -p "$tgt";'
        f' cp "{surse_r}"/* "$tgt/" 2>/dev/null;;',
        'esac',
        'exit 0',
        '']
    return "\n".join(linii)


def test_disc_fara_zona_refuza(tmp_path):
    with pytest.raises(SystemExit):  # argparse respinge fara --zona
        disc_r.main([])


def test_disc_password_lipsa_refuza(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(tmp_path)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and out["verdict"] == "REFUZ"
    assert out["tip"] == "PROBĂ PE DISC" and out["s3"] == "NEEXECUTAT/HOLD"


def test_disc_restic_bin_lipsa_refuza(tmp_path, capsys, monkeypatch):
    """Bare-name inexistent pe PATH → refuz inainte de orice efect."""
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(tmp_path / "zona"), extra=[
        "--restic-bin", "restic-care-nu-exista-xyz"]))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "restic" in out["motiv"]
    assert not os.path.exists(str(tmp_path / "zona"))


def test_disc_repo_sibling_refuzat(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona), repo=str(tmp_path / "repo-strain")))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "afara zonei" in out["motiv"]
    assert not os.path.exists(str(zona))


def test_disc_repo_traversal_refuzat(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(
        str(zona), repo=os.path.join(str(zona), "..", "evadat")))
    assert cod == 2


def test_disc_repo_symlink_refuzat(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "in-afara").mkdir()
    (tmp_path / "zona-link").symlink_to(tmp_path / "in-afara")
    cod = disc_r.main(_argv(str(zona), repo=str(tmp_path / "zona-link"
                                               / "repo")))
    assert cod == 2


def test_disc_repo_strain_refuzat(tmp_path, capsys, monkeypatch):
    """Repo existent negol fara config restic → refuz, nu suprascriere."""
    zona = tmp_path / "zona"
    (zona / "repo-test").mkdir(parents=True)
    (zona / "repo-test" / "fisier-strain.txt").write_text("x")
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "strain" in out["motiv"]
    assert (zona / "repo-test" / "fisier-strain.txt").exists()


def test_disc_repo_valid_strain_refuzat(tmp_path, capsys, monkeypatch):
    """Repo restic VALID dar fara marcajul probei → refuz: `forget
    --prune` al adaptorului ar sterge snapshoturi care nu ne apartin."""
    zona = tmp_path / "zona"
    (zona / "repo-test").mkdir(parents=True)
    (zona / "repo-test" / "config").write_text("{}")
    (zona / "repo-test" / "data").mkdir()  # arata ca un repo real
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "strain" in out["motiv"]


def test_disc_repo_fisier_refuzat(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    zona.mkdir(parents=True)
    (zona / "repo-test").write_text("nu e director")
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert disc_r.main(_argv(str(zona))) == 2


def test_disc_repo_dedicat_acceptat(tmp_path, capsys, monkeypatch):
    """Repo restic cu config + marcajul probei — dedicat, refolosit."""
    zona = tmp_path / "zona"
    (zona / "repo-test").mkdir(parents=True)
    (zona / "repo-test" / "config").write_text("{}")
    (zona / "repo-test" / ".proba-disc-f5").write_text("proba-disc-f5\n")
    _fixturi(tmp_path, restic_continut=_spy_cu_surat(str(zona)))
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 0 and out["verdict"] == "PASS", out


def test_disc_suprapuneri_refuzate(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    acelasi = str(zona / "acelasi")
    cod = disc_r.main(_argv(str(zona), repo=acelasi, extra=[
        "--sources-dir", acelasi]))
    assert cod == 2


def test_disc_director_pregatit_refuzat(tmp_path, capsys, monkeypatch):
    """surse/restore/out pre-existente negole → refuz (dedicat, curat)."""
    zona = tmp_path / "zona"
    (zona / "surse").mkdir(parents=True)
    (zona / "surse" / "deja-aici.txt").write_text("x")
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    assert cod == 2


def test_disc_zona_radacina_refuzata(tmp_path, capsys, monkeypatch):
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert disc_r.main(_argv("/")) == 2


def test_disc_retentie_invalida_refuza(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    for k in ("abc", "9999", "-1", "1.5", "٣"):
        cod = disc_r.main(_argv(str(zona), extra=["--keep-daily", k]))
        assert cod == 2, k


def test_disc_retentie_zero_zero_refuza(tmp_path, capsys, monkeypatch):
    """0/0 = forget --prune sterge snapshotul probei → refuz direct."""
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona), extra=[
        "--keep-daily", "0", "--keep-weekly", "0"]))
    assert cod == 2


def test_disc_adapter_hash_gresit_refuza(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona), extra=[
        "--adapter-sha256", "0" * 64]))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "adaptor" in out["motiv"]


def test_disc_adapter_sha_gol_refuza(tmp_path, capsys, monkeypatch):
    """--adapter-sha256 "" nu poate dezactiva pinul — format invalid."""
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona), extra=["--adapter-sha256", ""]))
    assert cod == 2


def test_disc_cale_cu_newline_refuzata(tmp_path, capsys, monkeypatch):
    """\\n in --repository ar injecta o a doua sursa in BACKUP_SOURCES
    (protocol linie-cu-linie) → sursa din afara zonei."""
    zona = tmp_path / "zona"
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona), repo=str(zona / "repo\nstrain")))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and "control" in out["motiv"]


def test_disc_marcaj_continut_gresit_refuza(tmp_path, capsys, monkeypatch):
    """config + fisier cu numele marcajului dar continut altul → strain."""
    zona = tmp_path / "zona"
    (zona / "repo-test").mkdir(parents=True)
    (zona / "repo-test" / "config").write_text("{}")
    (zona / "repo-test" / ".proba-disc-f5").write_text("altceva\n")
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    assert cod == 2


def test_disc_zona_fisier_refuzata(tmp_path, capsys, monkeypatch):
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert disc_r.main(_argv(str(tmp_path / "pf"))) == 2


def test_disc_cache_symlink_refuzat(tmp_path, capsys, monkeypatch):
    """.cache-restic pre-pus ca symlink → scrieri in afara zonei."""
    zona = tmp_path / "zona"
    zona.mkdir(parents=True)
    (zona / ".cache-restic").symlink_to(tmp_path / "in-afara")
    _fixturi(tmp_path)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    assert cod == 2


def test_disc_pozitiv_spy(tmp_path, capsys, monkeypatch):
    """Control pozitiv cu procese instrumentate: lantul intreg trece —
    init/backup-adaptor/snapshot-legat-de-surse/restore/hash. Env-ul
    mostenit NU trece la subprocese."""
    monkeypatch.setenv("BACKUP_EXTRA_ARGS", "--evil-env")
    monkeypatch.setenv("BACKUP_SOURCES", "/var/lib/docker/volumes/prod")
    monkeypatch.setenv("RESTIC_REPOSITORY", "s3:prod/repo")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "cheie-secret")
    zona = tmp_path / "zona"
    spy_log = tmp_path / "spy.log"
    adapter_continut = textwrap.dedent(f"""\
        #!/bin/sh
        echo "ADAPTER-EXTRA=$BACKUP_EXTRA_ARGS" >> "{spy_log}"
        echo "ADAPTER-SOURCES=$BACKUP_SOURCES" >> "{spy_log}"
        echo "ADAPTER-REPO=$RESTIC_REPOSITORY" >> "{spy_log}"
        echo "ADAPTER-AWS=$AWS_SECRET_ACCESS_KEY" >> "{spy_log}"
        "$RESTIC_BIN" backup $BACKUP_SOURCES
        "$RESTIC_BIN" forget --keep-daily $BACKUP_KEEP_DAILY --prune
        exit 0
    """)
    _fixturi(tmp_path,
             restic_continut=_spy_cu_surat(str(zona), log=spy_log),
             adapter_continut=adapter_continut)
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    log = spy_log.read_text() if spy_log.exists() else ""
    assert cod == 0 and out["verdict"] == "PASS", out
    assert "ADAPTER-EXTRA=" in log and "--evil-env" not in log
    assert "ADAPTER-AWS=" in log and "cheie-secret" not in log
    assert "prod/repo" not in log
    assert "volumes/prod" not in log
    assert f"ADAPTER-REPO={os.path.realpath(str(zona / 'repo-test'))}" in log
    assert "restore snap-disc" in log and "latest" not in log
    # niciun restic call cu --read-data-subset invalid
    assert "--read-data-subset" not in log
    # marcajul probei scris in repo
    assert Path(zona, "repo-test", ".proba-disc-f5").exists()
    # raportul e in zona, cu etichetele obligatorii
    rap = Path(zona, "raport-disc", "PROBA-PE-DISC.json")
    assert rap.exists()
    assert '"PROBĂ PE DISC"' in rap.read_text()


def test_disc_init_esuat_check_confirma(tmp_path, capsys, monkeypatch):
    """init rc≠0 pe repo dedicat + check independent rc=0 → continua."""
    zona = tmp_path / "zona"
    (zona / "repo-test").mkdir(parents=True)
    (zona / "repo-test" / "config").write_text("{}")
    (zona / "repo-test" / ".proba-disc-f5").write_text("proba-disc-f5\n")
    spy_log = tmp_path / "spy.log"
    _fixturi(tmp_path, restic_continut=_spy_cu_surat(
        str(zona), log=spy_log).replace(
        'case "$*" in', 'case "$*" in\n  *" init"*) exit 10;;'))
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 0 and out["verdict"] == "PASS", out
    # `check` real invocat ca comanda (ultimul cuvant din argv), nu ca
    # fragment din calea tmp.
    assert any(lin.split() and lin.split()[-1] == "check"
               for lin in spy_log.read_text().splitlines())


def test_disc_init_esuat_fara_check_esueaza(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path, restic_continut=textwrap.dedent("""\
        #!/bin/sh
        case "$*" in
          *" init"*) exit 1;;
          *"check"*) exit 1;;
        esac
        exit 0
    """))
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"


def test_disc_backup_esuat_nu_pass(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    spy_continut = _spy_cu_surat(str(zona))
    _fixturi(tmp_path, restic_continut=spy_continut,
             adapter_continut="#!/bin/sh\nexit 7\n")
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"
    pas = [p for p in out["pasi"] if "adaptorul" in p["pas"]]
    assert pas and not pas[0]["ok"]


def test_disc_snapshot_strain_nu_restaura(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    _fixturi(tmp_path, restic_continut=textwrap.dedent("""\
        #!/bin/sh
        case "$*" in
          *"snapshots"*) echo '[{"id":"strain","paths":["/alt/cine"]}]';;
        esac
        exit 0
    """))
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"


def test_disc_restore_esuat_nu_pass(tmp_path, capsys, monkeypatch):
    zona = tmp_path / "zona"
    surse_r = os.path.realpath(str(zona / "surse"))
    _fixturi(tmp_path, restic_continut=textwrap.dedent(f"""\
        #!/bin/sh
        case "$*" in
          *"snapshots"*) echo '[{{"id":"snap-disc","paths":["{surse_r}"]}}]';;
          *"restore"*) exit 9;;
        esac
        exit 0
    """))
    monkeypatch.chdir(tmp_path)
    cod = disc_r.main(_argv(str(zona)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"


@pytest.mark.skipif(
    RESTIC_REAL is None or not os.path.isfile(ADAPTOR_REAL),
    reason="restic real sau adaptorul vendoruit lipsesc — probă Mac")
def test_disc_pozitiv_restic_real_adaptor_produs(tmp_path, capsys):
    """Lantul REAL cap-coada pe Mac: restic 0.19.1 + adaptorul de
    productie vendoruit (restic-backup.sh byte-identic cu Hire@3f30640).
    init real → backup+forget real → snapshots real → restore real →
    hash identic, incl. fisierul cu spatii. Hashul pinat al adaptorului
    e verificat implicit (fara --adapter-sha256)."""
    zona = tmp_path / "zona"
    pf = tmp_path / "pf"
    pf.write_text("parola-test-123")
    cod = disc_r.main([
        "--zona", str(zona),
        "--repository", str(zona / "repo-test"),
        "--password-file", str(pf),
        "--adapter", ADAPTOR_REAL,
        "--restic-bin", RESTIC_REAL,
        "--keep-daily", "1", "--keep-weekly", "0"])
    out = json.loads(capsys.readouterr().out)
    assert cod == 0 and out["verdict"] == "PASS", out
    assert all(p["ok"] for p in out["pasi"])
    # repo-ul s-a creat real, cu config + marcajul probei
    assert Path(zona, "repo-test", "config").exists()
    assert Path(zona, "repo-test", ".proba-disc-f5").exists()
    # restaurate exacte, incl. fisierul cu spatii
    restaurate = list(Path(zona, "restore").rglob("*spatii*"))
    assert len(restaurate) == 1


@pytest.mark.skipif(
    RESTIC_REAL is None or not os.path.isfile(ADAPTOR_REAL),
    reason="restic real sau adaptorul vendoruit lipsesc — probă Mac")
def test_disc_refolosire_repo_real(tmp_path, capsys):
    """A doua rulare pe repo-ul creat de proba: config+marcaj → dedicat,
    init rc≠0 + check real rc=0 → continua si trece."""
    zona = tmp_path / "zona"
    pf = tmp_path / "pf"
    pf.write_text("parola-test-123")
    argv = ["--zona", str(zona), "--repository", str(zona / "repo-test"),
            "--password-file", str(pf), "--adapter", ADAPTOR_REAL,
            "--restic-bin", RESTIC_REAL,
            "--keep-daily", "1", "--keep-weekly", "0"]
    assert disc_r.main(argv) == 0
    capsys.readouterr()
    # restore si out sunt goale? nu — prima rulare le-a umplut → refuz.
    # Refolosirea reala cere director nou de restore/out:
    cod = disc_r.main(argv + ["--restore-dir", str(zona / "restore-2"),
                              "--out", str(zona / "raport-2"),
                              "--sources-dir", str(zona / "surse-2")])
    out = json.loads(capsys.readouterr().out)
    assert cod == 0 and out["verdict"] == "PASS", out


@pytest.mark.skipif(
    not os.path.isfile(os.path.join(
        os.path.realpath(os.path.join(
            REPO_ROOT, "..", "..", "BO-A03", "Hire-val4-01")),
        "backups", "restic-backup.sh")),
    reason="checkoutul Hire real exista numai pe Mac — drift-hash local")
def test_disc_adaptor_drift_vs_checkout_real():
    a = Path(ADAPTOR_REAL).read_bytes()
    b = Path(os.path.realpath(os.path.join(
        REPO_ROOT, "..", "..", "BO-A03", "Hire-val4-01")),
        "backups", "restic-backup.sh").read_bytes()
    assert a == b, "drift restic-backup.sh"
