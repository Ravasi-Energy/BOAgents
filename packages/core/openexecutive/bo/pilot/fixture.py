"""Synthetic signed package generator and HTTP service, explicit CLI only.

The fixed Ed25519 key is a PUBLIC TEST FIXTURE, never a production trust root.
Service credentials are supplied through BO_PILOT_FIXTURE_TOKENS JSON env only.
"""
import argparse
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from openexecutive.bo.packages.canon import signed_payload
from openexecutive.bo.packages.signing import sign
from openexecutive.bo.pilot.service import CONTENT, PACKAGE_ID


def package(work):
    import base64
    root = Path(work) / "package"
    (root / "bots").mkdir(parents=True, exist_ok=True)
    artifact = json.dumps(CONTENT, indent=2).encode()
    (root / "bots/diagnostic.json").write_bytes(artifact)
    key = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"PUBLIC SYNTHETIC ERP TEST KEY ONLY").digest())
    manifest = {"schemaVersion": "bo.package.v1", "packageId": PACKAGE_ID, "version": "1.0.0",
        "kind": "bobot", "publisherId": "pub-synthetic-erp", "keyId": "synthetic-test-key",
        "createdAt": "2026-09-26T00:00:00Z", "artifactDigests": {
            "bots/diagnostic.json": "sha256:" + hashlib.sha256(artifact).hexdigest()},
        "dependencies": [], "compatibility": {"minHost": "1.0.0", "maxHost": "1.0.0"},
        "requestedCapabilities": ["synth_erp:diagnose"], "settingsSchemaRef": None,
        "signature": {"algorithm": "ed25519", "value": ""}}
    manifest["signature"]["value"] = sign(key, signed_payload(manifest))
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
    registry = {"schemaVersion": "bo.package.registry.v1", "registryId": "synthetic-pilot-only", "version": "synthetic-1",
        "updatedAt": "2026-09-26T00:00:00Z", "publishers": [{"publisherId": "pub-synthetic-erp",
        "status": "active", "allowedKinds": ["bobot"], "keyIds": ["synthetic-test-key"]}],
        "keys": [{"keyId": "synthetic-test-key", "algorithm": "ed25519",
                  "publicKey": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode(),
                  "status": "active", "notBefore": "2026-01-01T00:00:00Z", "notAfter": None}],
        "policy": {"allowedKinds": ["bobot"], "capabilityCatalog": ["synth_erp:diagnose"],
                   "maxCapabilitiesPerKind": {"bobot": 1}, "maxPackageBytes": 65536,
                   "rollbackRequiresApproval": True, "policyVersion": "synthetic-1"}}
    (Path(work) / "registry.json").write_text(json.dumps(registry, indent=2))
    return root


def server(db_path, tokens, port=0):
    # tokens maps credential -> authoritative synthetic tenant; never from body.
    with sqlite3.connect(db_path) as conn:
        conn.executescript("""CREATE TABLE IF NOT EXISTS effects (
            tenant TEXT, key TEXT, digest TEXT, result TEXT, calls INTEGER,
            PRIMARY KEY(tenant,key));
            CREATE TABLE IF NOT EXISTS modes (tenant TEXT PRIMARY KEY, mode TEXT);""")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.handle_request(False)

        def do_POST(self):
            self.handle_request(True)

        def handle_request(self, post):
            tenant = tokens.get(self.headers.get("Authorization", "").removeprefix("Bearer "))
            if not tenant:
                return self.reply(401, {"error": "credential_required"})
            body = {}
            if post:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    return self.reply(413, {})
                try:
                    body = json.loads(self.rfile.read(length))
                except ValueError:
                    return self.reply(400, {})
            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                conn.execute("BEGIN IMMEDIATE")
                if self.path == "/scenario" and post:
                    if body.get("scenario") not in ("healthy", "delayed", "unknown", "recover", "unavailable"):
                        return self.reply(422, {})
                    conn.execute("INSERT INTO modes VALUES (?,?) ON CONFLICT(tenant) DO UPDATE SET mode=excluded.mode", (tenant, body["scenario"]))
                    conn.commit()
                    return self.reply(200, {"synthetic": True})
                mode_row = conn.execute("SELECT mode FROM modes WHERE tenant=?", (tenant,)).fetchone()
                mode = mode_row[0] if mode_row else "healthy"
                if mode == "unavailable" and self.path != "/stats":
                    return self.reply(503, {"error": "synthetic_service_unavailable"})
                if self.path == "/stats" and not post:
                    row = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(calls),0) AS calls FROM effects WHERE tenant=?", (tenant,)).fetchone()
                    return self.reply(200, {"effectCount": row["n"], "submitCalls": row["calls"]})
                if self.path.startswith("/receipts/") and not post:
                    from urllib.parse import unquote
                    key = unquote(self.path.removeprefix("/receipts/"))
                    row = conn.execute("SELECT result FROM effects WHERE tenant=? AND key=?", (tenant, key)).fetchone()
                    result = json.loads(row[0]) if row else {}
                elif self.path == "/probe" and post:
                    required = {"tenantRef", "idempotencyKey", "payloadDigest", "executionRef", "correlationId", "producerId", "installationId"}
                    if set(body) != required or not all(isinstance(v, str) and 0 < len(v) <= 128 for v in body.values()):
                        return self.reply(422, {})
                    if body["tenantRef"] != tenant:
                        return self.reply(403, {"error": "tenant_mismatch"})
                    row = conn.execute("SELECT * FROM effects WHERE tenant=? AND key=?", (tenant, body["idempotencyKey"])).fetchone()
                    if row and row["digest"] != body["payloadDigest"]:
                        return self.reply(409, {"error": "payload_conflict"})
                    if row:
                        result = json.loads(row["result"])
                        conn.execute("UPDATE effects SET calls=calls+1 WHERE tenant=? AND key=?", (tenant, body["idempotencyKey"]))
                    else:
                        now = datetime.now(UTC)
                        stamp = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
                        observed = (now - timedelta(seconds=600) if mode == "delayed" else now).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                        receipt = "rcp_" + uuid.uuid4().hex
                        result = {"receipt": {"tenant": tenant, "receipt_ref": receipt, "provider": "synth.erp",
                            "effect_key": body["idempotencyKey"], "digest": body["payloadDigest"], "amount": 1, "received_at": stamp},
                            "observation": {"schemaVersion": "bo.service-observation.v1", "eventId": "obs_" + uuid.uuid4().hex,
                            "producerId": body["producerId"], "installationId": body["installationId"], "tenantRef": tenant,
                            "product": "BOAgents", "observedAt": observed, "correlationId": body["correlationId"],
                            "service": {"serviceRef": "synthetic-erp", "ownerRef": "synthetic-owner", "version": "1.0.0",
                            "synthetic": True, "queuePending": 200 if mode == "delayed" else 0,
                            "oldestPendingAt": observed if mode == "delayed" else None, "executionRef": body["executionRef"], "evidenceRefs": [receipt]}}}
                        conn.execute("INSERT INTO effects VALUES (?,?,?,?,1)", (tenant, body["idempotencyKey"], body["payloadDigest"], json.dumps(result)))
                    conn.commit()
                else:
                    return self.reply(404, {})
                if mode == "unknown":
                    result["receipt"] = None
                return self.reply(200, result)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["package", "serve"])
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8325)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    if args.command == "package":
        print(package(args.work))
    else:
        tokens = json.loads(os.environ["BO_PILOT_FIXTURE_TOKENS"])
        httpd = server(args.work / "service.db", tokens, args.port)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()


if __name__ == "__main__":
    main()
