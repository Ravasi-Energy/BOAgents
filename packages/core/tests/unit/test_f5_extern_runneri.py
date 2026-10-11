"""@requirement HIR-REQ-138 — regresii runneri externi F5 (HOLD-OBLIO-1/S3-1).

Acopera defectele reproduse de coordonator (sibling --out, -test numai
in host) si intreaga matrice de preflight: traversal, symlink, bucket/
prefix gresit, suprapunere directoare, sanitizare env, init/check,
snapshot id exact — cu transport HTTP si procese restic instrumentate,
fara acces live.
"""
import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", ".."))
TOOLS = os.path.join(REPO_ROOT, "tools", "val4")
sys.path.insert(0, TOOLS)

import proba_extern_oblio as oblio_r  # noqa: E402
import proba_extern_s3 as s3_r  # noqa: E402

FIXTURA_HIRE = os.path.join(TOOLS, "fixtura_hire")
# Checkoutul real Hire (doar Mac) — in CI lipseste si hash-driftul se sare.
HIRE_REAL = os.path.realpath(os.path.join(
    REPO_ROOT, "..", "..", "BO-A03", "Hire-val4-01", "backend"))


def _mk_hire_backend(_root=None):
    """Adaptorul Hire REAL — copie byte-identica vendoruita in
    tools/val4/fixtura_hire (PIN.json), nu o implementare falsa.
    Identitatea cu checkoutul real e verificata pe Mac prin hash."""
    return FIXTURA_HIRE


class _RaspFals:
    status = 200

    def __init__(self, corp):
        self._corp = corp

    def read(self):
        return json.dumps(self._corp).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------- Oblio

def _argv_oblio(zona, backend=None, out=None, emit=False, client_nume="Client Sintetic SRL"):
    a = ["--base-url", "https://www.oblio.eu", "--client-id", "test-id",
         "--secret-ref", "OBLIO_TEST",
         "--hire-backend", backend or FIXTURA_HIRE,
         "--zona", os.path.join(zona, "extern")]
    if out:
        a += ["--out", out]
    if emit:
        a += ["--emit", "--firma-cui", "RO12345678"]
        if client_nume:
            a += ["--client-nume", client_nume]
    return a


def test_oblio_fara_secret_refuza_zero_http(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("CONECTOR_SECRET_OBLIO_TEST", raising=False)
    cod = oblio_r.main(_argv_oblio(str(tmp_path), _mk_hire_backend(str(tmp_path / "be"))))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and out["verdict"] == "REFUZ" and out["apeluri_http"] == 0


def test_oblio_out_sibling_refuzat(tmp_path, capsys, monkeypatch):
    """Cazul coordonatorului: <zona>/extern-foreign nu e in <zona>/extern."""
    monkeypatch.setenv("CONECTOR_SECRET_OBLIO_TEST", "x")
    be = _mk_hire_backend(str(tmp_path / "be"))
    cod = oblio_r.main(_argv_oblio(
        str(tmp_path), be, out=os.path.join(str(tmp_path), "extern-foreign")))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and out["verdict"] == "REFUZ" and out["apeluri_http"] == 0
    assert not os.path.exists(os.path.join(str(tmp_path), "extern-foreign"))


def test_oblio_out_traversal_refuzat(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CONECTOR_SECRET_OBLIO_TEST", "x")
    be = _mk_hire_backend(str(tmp_path / "be"))
    cod = oblio_r.main(_argv_oblio(
        str(tmp_path), be, out=os.path.join(str(tmp_path), "extern", "..", "esc")))
    assert cod == 2


def test_oblio_out_symlink_refuzat(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CONECTOR_SECRET_OBLIO_TEST", "x")
    be = _mk_hire_backend(str(tmp_path / "be"))
    tinta = tmp_path / "in-afara"
    tinta.mkdir()
    link = tmp_path / "extern" / "link"
    (tmp_path / "extern").mkdir()
    link.symlink_to(tinta)
    cod = oblio_r.main(_argv_oblio(str(tmp_path), be, out=str(link)))
    assert cod == 2


def test_oblio_http_refuzat(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CONECTOR_SECRET_OBLIO_TEST", "x")
    a = _argv_oblio(str(tmp_path), _mk_hire_backend(str(tmp_path / "be")))
    a[1] = "http://www.oblio.eu"
    assert oblio_r.main(a) == 2


def test_oblio_emit_fara_cui_refuzat(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("CONECTOR_SECRET_OBLIO_TEST", "x")
    a = _argv_oblio(str(tmp_path), _mk_hire_backend(str(tmp_path / "be")),
                    out=None, emit=False) + ["--emit"]
    assert oblio_r.main(a) == 2


def test_oblio_pozitiv_transport_instrumentat(tmp_path, capsys, monkeypatch):
    """Adaptorul Hire REAL pe transport instrumentat la limita externa:
    auth → listare → emitere proforma cu schema reala (cif_emitent,
    client.nume, moneda, serie) → readback EXACT al referintei emise."""
    monkeypatch.setenv("CONECTOR_SECRET_OBLIO_TEST", "x")

    apeluri = []
    corpi = []

    import urllib.error

    def fals(req, *a, **kw):
        apeluri.append((req.get_method(), req.full_url))
        corp = req.data or b"{}"
        if b"valoare-gresita-deliberat" in corp:
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {}, None)
        if "authorize/token" in req.full_url:
            corpi.append(json.loads(corp))
            return _RaspFals({"access_token": "tok"})
        if req.get_method() == "POST" and "docs/proforma" in req.full_url:
            corpi.append(json.loads(req.data))
            # raspuns ca Oblio: ref = serie-numar, cerut la readback
            return _RaspFals({"seriesName": "PROBA", "number": "7",
                              "id": 7007, "link": "x"})
        return _RaspFals({"data": [{"seriesName": "PROBA", "number": "7",
                                    "id": 7007}]})

    monkeypatch.setattr(oblio_r, "_urlopen_real", fals)
    cod = oblio_r.main(_argv_oblio(str(tmp_path), emit=True))
    out = json.loads(capsys.readouterr().out)
    assert cod == 0 and out["verdict"] == "PASS", out
    assert out["proforme_emise"] == 1 and out["apeluri_http"] >= 4
    assert all(u.startswith("https://") for _m, u in apeluri)
    # contractul de request REAL: schema adaptorului, nu nume inventate
    emis = [c for c in corpi if "products" in c]
    assert len(emis) == 1
    assert emis[0]["cif"] == "RO12345678"
    assert emis[0]["client"]["name"] == "Client Sintetic SRL"
    assert emis[0]["client"].get("email") in (None, "")
    assert emis[0]["currency"] == "RON" and emis[0]["seriesName"] == "PROBA"
    assert emis[0]["document"] == "proforma"
    assert emis[0]["idempotency"].startswith("proba-extern-")
    # readback exact al referintei emise
    pas = [p for p in out["pasi"] if p["pas"].startswith("readback")]
    assert pas and all(p["ok"] and p["detalii"] == "gasit" for p in pas)
    # tokenul si secretul nu ajung in raport
    assert "tok" not in json.dumps(out) and "client_secret" not in json.dumps(out)


def test_oblio_emit_fara_client_refuza_inainte_http(tmp_path, capsys,
                                                   monkeypatch):
    """Regresia coordonatorului: --emit cu date incomplete trebuie REFUZ
    inainte de HTTP — nu KeyError 'cif_emitent'/'client.nume' dupa auth."""
    monkeypatch.setenv("CONECTOR_SECRET_OBLIO_TEST", "x")
    apeluri = []

    def fals(req, *a, **kw):
        apeluri.append(req.full_url)
        return _RaspFals({"access_token": "tok"})

    monkeypatch.setattr(oblio_r, "_urlopen_real", fals)
    # fara --client-nume → refuz de validare, zero trafic
    cod = oblio_r.main(_argv_oblio(str(tmp_path), emit=True, client_nume=""))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and out["verdict"] == "REFUZ"
    assert apeluri == [] and out["apeluri_http"] == 0


def test_oblio_secret_gresit_clasat_auth(tmp_path, capsys, monkeypatch):
    """Refuzul 401 vine DIN TRANSPORT (adaptor real), nu din cod local —
    trebuie clasat 'auth', nu outage/raspuns invalid."""
    import urllib.error

    monkeypatch.setenv("CONECTOR_SECRET_OBLIO_TEST", "x")

    def fals(req, *a, **kw):
        corp = req.data or b"{}"
        if b"valoare-gresita-deliberat" in corp:
            raise urllib.error.HTTPError(
                req.full_url, 401, "Unauthorized", {}, None)
        if "authorize/token" in req.full_url:
            return _RaspFals({"access_token": "tok"})
        return _RaspFals({"data": []})

    monkeypatch.setattr(oblio_r, "_urlopen_real", fals)
    cod = oblio_r.main(_argv_oblio(str(tmp_path)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 0 and out["verdict"] == "PASS"
    pas = [p for p in out["pasi"] if p["pas"] == "refuz pe secret gresit"]
    assert pas and pas[0]["ok"] and "auth" in pas[0]["detalii"]


def test_oblio_outage_clasat_distinct(tmp_path, capsys, monkeypatch):
    """Outage-ul (URLError) nu se claseaza auth si nu devine PASS."""
    import urllib.error

    monkeypatch.setenv("CONECTOR_SECRET_OBLIO_TEST", "x")

    def fals(req, *a, **kw):
        raise urllib.error.URLError("simulated outage")

    monkeypatch.setattr(oblio_r, "_urlopen_real", fals)
    cod = oblio_r.main(_argv_oblio(str(tmp_path)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"
    pas = [p for p in out["pasi"] if p["pas"] == "lant adaptor"]
    assert pas and not pas[0]["ok"] and "outage" in pas[0]["detalii"]


def test_fixtura_hire_hash_pin():
    """Fixture-ul vendoruit este byte-identic cu pinul consemnat —
    nu o 'implementare aproximativa' a adaptorului."""
    pin = json.loads(Path(FIXTURA_HIRE, "PIN.json").read_text())
    for rel, sha in pin["sha256"].items():
        assert sha_fisier_local(Path(FIXTURA_HIRE, rel)) == sha, rel


def sha_fisier_local(p):
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.mark.skipif(
    not os.path.isfile(os.path.join(
        HIRE_REAL, "hire", "connectors", "serviciu", "providers", "oblio.py")),
    reason="checkoutul Hire real exista numai pe Mac — drift-hash local")
def test_fixtura_hire_drift_vs_checkout_real():
    """Pe Mac: copiile vendoruite raman byte-identice cu adaptorul real —
    orice drift intre fixtura si produs cade aici."""
    for rel in ("connectors/base.py",
                "connectors/serviciu/providers/baza.py",
                "connectors/serviciu/providers/oblio.py",
                "connectors/serviciu/secrete.py"):
        a = Path(FIXTURA_HIRE, "hire", rel)
        b = Path(HIRE_REAL, "hire", rel)
        assert a.read_bytes() == b.read_bytes(), f"drift: {rel}"


# ------------------------------------------------------------------- S3

def _argv_s3(zona, repo="s3:fsn1.your-objectstorage.com/bucket-test/prefix",
             extra=()):
    a = ["--repository", repo, "--password-file", "pf",
         "--restic-bin", "restic-spy", "--adapter", "adapter.sh",
         "--zona", os.path.join(zona, "extern")] + list(extra)
    return a


def _fixturi_s3(tmp_path, restic_continut=None):
    """password-file + restic spy + adaptor minimal."""
    pf = tmp_path / "pf"
    pf.write_text("parola-test")
    spy = tmp_path / "restic-spy"
    spy.write_text(restic_continut or "#!/bin/sh\necho spy >> \"$SPY_LOG\"\nexit 0\n")
    spy.chmod(0o755)
    ad = tmp_path / "adapter.sh"
    ad.write_text("#!/bin/sh\nexit 0\n")
    ad.chmod(0o755)
    return str(pf), str(spy), str(ad)


def _s3_env(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ak-test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk-test")


def test_s3_fara_aws_refuza(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)
    pf, spy, ad = _fixturi_s3(tmp_path)
    os.chdir(tmp_path)
    cod = s3_r.main(_argv_s3(str(tmp_path)))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and out["verdict"] == "REFUZ"


def test_s3_out_sibling_refuzat(tmp_path, capsys, monkeypatch):
    """Cazul coordonatorului: <zona>/extern-foreign."""
    _s3_env(monkeypatch)
    pf, spy, ad = _fixturi_s3(tmp_path)
    os.chdir(tmp_path)
    cod = s3_r.main(_argv_s3(str(tmp_path), extra=[
        "--out", os.path.join(str(tmp_path), "extern-foreign")]))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and out["verdict"] == "REFUZ"
    assert not os.path.exists(os.path.join(str(tmp_path), "extern-foreign"))


def test_s3_test_doar_in_host_refuzat(tmp_path, capsys, monkeypatch):
    """Cazul coordonatorului: s3:host-test/production/live — marcajul e
    numai in host, nu in bucket/prefix."""
    _s3_env(monkeypatch)
    pf, spy, ad = _fixturi_s3(tmp_path)
    os.chdir(tmp_path)
    cod = s3_r.main(_argv_s3(
        str(tmp_path), repo="s3:fsn1.your-objectstorage.com-test/production/live"))
    out = json.loads(capsys.readouterr().out)
    assert cod == 2 and out["verdict"] == "REFUZ"


def test_s3_endpoint_gresit_refuzat(tmp_path, capsys, monkeypatch):
    _s3_env(monkeypatch)
    pf, spy, ad = _fixturi_s3(tmp_path)
    os.chdir(tmp_path)
    cod = s3_r.main(_argv_s3(
        str(tmp_path), repo="s3:alt-host.example.com/bucket-test/p"))
    assert cod == 2


def test_s3_bucket_fara_test_refuzat(tmp_path, capsys, monkeypatch):
    _s3_env(monkeypatch)
    pf, spy, ad = _fixturi_s3(tmp_path)
    os.chdir(tmp_path)
    cod = s3_r.main(_argv_s3(
        str(tmp_path), repo="s3:fsn1.your-objectstorage.com/bucket-prod/live"))
    assert cod == 2


def test_s3_directoare_suprapuse_refuzate(tmp_path, capsys, monkeypatch):
    _s3_env(monkeypatch)
    pf, spy, ad = _fixturi_s3(tmp_path)
    os.chdir(tmp_path)
    acelasi = os.path.join(str(tmp_path), "extern", "acelasi")
    cod = s3_r.main(_argv_s3(str(tmp_path), extra=[
        "--sources-dir", acelasi, "--restore-dir", acelasi]))
    assert cod == 2


def test_s3_pozitiv_preflight_si_spy(tmp_path, capsys, monkeypatch):
    """Control pozitiv: config autorizata trece preflight si cheama
    adaptorul + restic; snapshotul exact se restaureaza, nu 'latest';
    env-ul mostenit (BACKUP_EXTRA_ARGS) NU trece la subprocese."""
    _s3_env(monkeypatch)
    monkeypatch.setenv("BACKUP_EXTRA_ARGS", "--evil-din-env")
    spy_log = tmp_path / "spy.log"

    surse_r = os.path.realpath(os.path.join(str(tmp_path), "extern", "surse"))
    spy_continut = textwrap.dedent(f"""\
        #!/bin/sh
        echo "$@" >> "{spy_log}"
        echo "BACKUP_EXTRA_ARGS=$BACKUP_EXTRA_ARGS" >> "{spy_log}"
        case "$*" in
          *"snapshots"*) echo '[{{"id":"snap123","time":"2026-10-11T17:00:00Z","paths":["{surse_r}"]}},{{"id":"snap-old","time":"2026-01-01T00:00:00Z","paths":["/alt/cineva"]}}]';;
          *"restore"*) eval "tgt=\\$$#"; mkdir -p "$tgt";
                       cp "{surse_r}"/* "$tgt/" 2>/dev/null;;
          *"check"*) exit 0;;
        esac
        exit 0
    """)
    pf, spy, ad = _fixturi_s3(tmp_path, restic_continut=spy_continut)
    os.chdir(tmp_path)
    cod = s3_r.main(_argv_s3(str(tmp_path), extra=["--restic-bin", spy]))
    out = json.loads(capsys.readouterr().out)
    log = spy_log.read_text() if spy_log.exists() else ""
    assert cod == 0 and out["verdict"] == "PASS", out
    # env sanitizat: --evil-din-env nu trece la subprocese
    assert "--evil-din-env" not in log
    # restore pe id-ul exact legat de sursele curente, nu 'latest' si
    # nu snapshotul cuiva (paths diferit)
    assert "restore snap123" in log and "restore snap-old" not in log
    assert "latest" not in log.replace("snap123", "")


def test_s3_snapshot_strain_refuzat(tmp_path, capsys, monkeypatch):
    """Lista de snapshoturi fara niciunul legat de sursele curente → FAIL,
    nu restore pe id arbitrar."""
    _s3_env(monkeypatch)
    spy_continut = textwrap.dedent("""\
        #!/bin/sh
        case "$*" in
          *"snapshots"*) echo '[{"id":"snap-foreign","paths":["/alt/bucket"]}]';;
        esac
        exit 0
    """)
    pf, spy, ad = _fixturi_s3(tmp_path, restic_continut=spy_continut)
    os.chdir(tmp_path)
    cod = s3_r.main(_argv_s3(str(tmp_path), extra=["--restic-bin", spy]))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"
    pas = [p for p in out["pasi"] if "snapshots" in p["pas"]]
    assert pas and not pas[0]["ok"]


def test_s3_init_esuat_fara_check_esueaza(tmp_path, capsys, monkeypatch):
    """init nezero fara confirmare independenta → FAIL, nu PASS."""
    _s3_env(monkeypatch)
    spy_log = tmp_path / "spy.log"
    monkeypatch.setenv("SPY_LOG", str(spy_log))
    spy_continut = textwrap.dedent("""\
        #!/bin/sh
        case "$*" in
          *" init"*) exit 1;;
          *"check"*) exit 1;;
        esac
        exit 0
    """)
    pf, spy, ad = _fixturi_s3(tmp_path, restic_continut=spy_continut)
    os.chdir(tmp_path)
    cod = s3_r.main(_argv_s3(str(tmp_path), extra=["--restic-bin", spy]))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"
    pas = [p for p in out["pasi"] if p["pas"] == "restic init"]
    assert pas and not pas[0]["ok"]


def test_s3_snapshots_rc_nezero_esueaza(tmp_path, capsys, monkeypatch):
    _s3_env(monkeypatch)
    spy_continut = textwrap.dedent("""\
        #!/bin/sh
        case "$*" in
          *"snapshots"*) exit 1;;
        esac
        exit 0
    """)
    pf, spy, ad = _fixturi_s3(tmp_path, restic_continut=spy_continut)
    os.chdir(tmp_path)
    cod = s3_r.main(_argv_s3(str(tmp_path), extra=["--restic-bin", spy]))
    out = json.loads(capsys.readouterr().out)
    assert cod == 1 and out["verdict"] == "FAIL"
    pas = [p for p in out["pasi"] if "snapshots" in p["pas"]]
    assert pas and not pas[0]["ok"]
