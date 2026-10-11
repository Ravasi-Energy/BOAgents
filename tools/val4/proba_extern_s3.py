#!/usr/bin/env python3
"""Runner executabil al probei externe S3/Hetzner (HOLD-S3-1, HIR-REQ-040).

Sursa canonica: tools/val4/proba_extern_s3.py (repo BOAgents).
Copia din agenti/BO-A01/instrumente-f5/ este derivata, cu hash consemnat.

Se ruleaza numai cand exista acces de test autorizat:

    AWS_ACCESS_KEY_ID=<key-test> AWS_SECRET_ACCESS_KEY=<secret-test> \
    python3 proba_extern_s3.py \
        --repository s3:fsn1.your-objectstorage.com/<bucket-test>/<prefix> \
        --password-file <fisier-parola-repo> \
        --adapter <cale backups/restic-backup.sh> \
        --restic-bin <cale-restic> \
        --out <dir-propriu>

Proba: surse sintetice generate in zona proprie → backup prin adaptorul
de productie (env construit EXCLUSIV de runner — BACKUP_EXTRA_ARGS/
BACKUP_EXCLUDE mostenite sunt eliminate, nu propagate) → `snapshots`
(exitcode + parsare stricta + identificator) → `forget --prune` →
`restore <snapshot-id>`, nu `latest` arbitrar → comparatie hash.

Refuzuri INAINTE de orice invocare restic (exit 2, zero efect):
- AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY lipsesc
- RESTIC_PASSWORD_FILE / RESTIC_BIN / adaptor lipsesc
- repository parse esuat, endpoint != cel autorizat (--endpoint),
  sau marcaj `-test` lipsa din BUCKET/PREFIX (numele hostului NU
  este autorizatie de cont)
- --out / --sources-dir / --restore-dir fara containment real in
  --zona (resolve+commonpath; sibling/traversal/symlink refuzate)
- cele trei directoare se suprapun (sursa in restore etc.)

`restic init` esuat continua NUMAI daca `restic check` independent
confirma repository-ul deja initializat; orice auth/network/parse
failure e FAIL, nu PASS. Raportul nu contine secrete: detaliile sunt
trunchiate si elidate.
"""
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
_ZONA_DEFAULT = os.path.join(_ROOT, "extern")

_ENDPOINT_IMPLICIT = "fsn1.your-objectstorage.com"
# Variabilele pe care runnerul le seteaza explicit; orice altceva din
# env mostenit nu trece la adaptor/subprocese (fara BACKUP_EXTRA_ARGS
# sau BACKUP_EXCLUDE de provenienta necunoscuta).
_ENV_MINIM = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ",
              "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
              "AWS_DEFAULT_REGION", "AWS_REGION")


def _curata(text) -> str:
    t = re.sub(r'(access_token|aws_secret_access_key|password'
               r'["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+', r'\1***', str(text))
    return t[:200]


def in_zona(cale: str, zona: str) -> bool:
    try:
        rz = os.path.realpath(zona)
        r = os.path.realpath(cale)
        return os.path.commonpath([r, rz]) == rz
    except (ValueError, OSError):
        return False


def straine(a: str, b: str) -> bool:
    """Niciunul nu contine si nu coincide cu celalalt."""
    ra, rb = os.path.realpath(a), os.path.realpath(b)
    if ra == rb:
        return False
    try:
        return (os.path.commonpath([ra, rb]) != ra
                and os.path.commonpath([ra, rb]) != rb)
    except ValueError:
        return True


def refuza(motiv: str, cod: int = 2) -> int:
    print(json.dumps({"verdict": "REFUZ", "motiv": _curata(motiv),
                      "efect": "zero"}))
    return cod


def sha_fisier(cale: str) -> str:
    h = hashlib.sha256()
    with open(cale, "rb") as f:
        for buc in iter(lambda: f.read(1 << 20), b""):
            h.update(buc)
    return h.hexdigest()


def parse_repo(repo: str):
    """s3:<host>/<bucket>[/<prefix>] → (host, bucket, prefix) sau None."""
    m = re.fullmatch(r"s3:([^/]+)/([^/\s]+)(/\S*)?", repo)
    if not m:
        return None
    return m.group(1), m.group(2), (m.group(3) or "").lstrip("/")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Proba externa S3/Hetzner (HOLD-S3-1) — numai cu acces autorizat")
    p.add_argument("--repository", required=True,
                   help="s3:<endpoint>/<bucket>[/<prefix>]; -test obligatoriu "
                        "in bucket sau prefix (NU numai in host)")
    p.add_argument("--endpoint", default=_ENDPOINT_IMPLICIT,
                   help="endpointul S3 autorizat (Hetzner suveran)")
    p.add_argument("--password-file", required=True)
    p.add_argument("--restic-bin", default="restic")
    p.add_argument("--adapter", required=True,
                   help="calea catre backups/restic-backup.sh din checkoutul Hire")
    p.add_argument("--zona", default=_ZONA_DEFAULT,
                   help="zona proprie permisa pentru surse/restore/out")
    p.add_argument("--sources-dir", default=None, help="implicit <zona>/surse")
    p.add_argument("--restore-dir", default=None, help="implicit <zona>/restore")
    p.add_argument("--out", default=None, help="implicit <zona>/raport-s3")
    args = p.parse_args(argv)

    surse = args.sources_dir or os.path.join(args.zona, "surse")
    restore = args.restore_dir or os.path.join(args.zona, "restore")
    out = args.out or os.path.join(args.zona, "raport-s3")

    # --- refuzuri inainte de efect ----------------------------------
    for nume in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        if not os.environ.get(nume):
            return refuza(f"{nume} neprovisionat — acces lipsa, zero efect")

    repo = parse_repo(args.repository)
    if repo is None:
        return refuza("--repository nu respecta s3:<host>/<bucket>[/<prefix>]")
    host, bucket, prefix = repo
    if host != args.endpoint:
        return refuza(f"endpoint {host!r} != endpointul autorizat {args.endpoint!r}")
    if "-test" not in bucket and "-test" not in prefix:
        return refuza("marcaj -test lipsa din bucket/prefix — "
                      "numele hostului nu e autorizatie")

    if not os.path.isfile(args.password_file):
        return refuza("--password-file inexistent")
    if os.path.dirname(args.restic_bin) and not os.path.isfile(args.restic_bin):
        return refuza("--restic-bin inexistent")
    if not os.path.isfile(args.adapter):
        return refuza("adaptorul de productie lipseste (--adapter)")
    for nume, cale in (("sources-dir", surse), ("restore-dir", restore),
                       ("out", out)):
        if not in_zona(cale, args.zona):
            return refuza(f"--{nume} in afara zonei proprii "
                          "(sibling/traversal/symlink)")
    for a, b in ((surse, restore), (surse, out), (restore, out)):
        if not straine(a, b):
            return refuza("--sources-dir/--restore-dir/--out se suprapun")

    env = {k: os.environ[k] for k in _ENV_MINIM if k in os.environ}
    env.update(RESTIC_BIN=args.restic_bin,
               RESTIC_REPOSITORY=args.repository,
               RESTIC_PASSWORD_FILE=args.password_file,
               BACKUP_SOURCES=os.path.realpath(surse),
               BACKUP_KEEP_DAILY="1", BACKUP_KEEP_WEEKLY="0",
               BACKUP_EXTRA_ARGS="", BACKUP_EXCLUDE="")

    raport = {"verdict": "PASS", "pasi": [], "repository": args.repository,
              "endpoint": host, "bucket": bucket, "prefix": prefix}

    def pas(nume, ok, detalii=""):
        raport["pasi"].append({"pas": nume, "ok": bool(ok),
                               "detalii": _curata(detalii)})
        if not ok:
            raport["verdict"] = "FAIL"

    def ruleaza(cmd):
        return subprocess.run(cmd, env=env, capture_output=True, text=True)

    try:
        surse_r = os.path.realpath(surse)
        os.makedirs(surse_r, exist_ok=True)
        os.makedirs(os.path.realpath(restore), exist_ok=True)
        os.makedirs(os.path.realpath(out), exist_ok=True)
        for i in range(3):
            with open(os.path.join(surse_r, f"sintetic-{i}.txt"), "w") as f:
                f.write(f"proba-extern-s3 continut sintetic {i}\n")
        hash_inainte = {f: sha_fisier(os.path.join(surse_r, f))
                        for f in sorted(os.listdir(surse_r))}
        pas("surse sintetice generate", len(hash_inainte) == 3)

        r = ruleaza([args.restic_bin, "-r", args.repository, "init"])
        if r.returncode == 0:
            pas("restic init", True)
        else:
            # Nezero nu e «deja initializat» implicit — doar un check
            # independent pe acelasi repository confirma repo-ul viu.
            c = ruleaza([args.restic_bin, "-r", args.repository,
                         "check", "--read-data-subset=0%"])
            pas("restic init", c.returncode == 0,
                f"init rc={r.returncode}, check rc={c.returncode}: "
                + _curata((r.stderr or r.stdout)[:120]))
            if c.returncode != 0:
                raise RuntimeError("init esuat si repo-ul nu e confirmat")

        r = ruleaza(["bash", args.adapter])
        pas("backup prin adaptorul de productie", r.returncode == 0,
            (r.stderr or r.stdout).strip().splitlines()[-1:])

        r = ruleaza([args.restic_bin, "-r", args.repository,
                     "snapshots", "--json"])
        snap_id = None
        if r.returncode == 0:
            try:
                snap = json.loads(r.stdout)
                if isinstance(snap, list) and snap:
                    snap_id = snap[-1].get("id") or snap[-1].get("short_id")
            except Exception:
                pass
        pas("snapshots exitcode+lista+identificator", bool(snap_id),
            f"rc={r.returncode} id={snap_id}")
        if not snap_id:
            raise RuntimeError("niciun snapshot identificat")

        r = ruleaza([args.restic_bin, "-r", args.repository,
                     "restore", snap_id, "--target",
                     os.path.realpath(restore)])
        pas("restore snapshotul exact", r.returncode == 0,
            (r.stderr or r.stdout).strip().splitlines()[-1:])

        restaurate = {}
        for radacina, _d, fisiere in os.walk(os.path.realpath(restore)):
            for f in fisiere:
                restaurate[f] = sha_fisier(os.path.join(radacina, f))
        ok = all(restaurate.get(f) == h for f, h in hash_inainte.items())
        pas("hash restore == sursa", ok, json.dumps(sorted(restaurate))[:160])
    except Exception as e:
        pas("lant proba", False, _curata(e))
        raport["verdict"] = "FAIL"

    with open(os.path.join(os.path.realpath(out),
                           "PROBA-EXTERN-S3.json"), "w") as f:
        json.dump(raport, f, indent=1, ensure_ascii=False)
    print(json.dumps(raport, ensure_ascii=False))
    return 0 if raport["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
