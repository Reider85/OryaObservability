"""mTLS tests (PP02 / T3.0.2).

Covers two areas without requiring Docker:

1. SDK-side mTLS: ``LangfuseExporter`` connects to an in-process OTLP/Langfuse
   endpoint that requires a verified client certificate. A private PKI
   (CA + server cert + valid/expired client certs) is minted in-process with
   ``cryptography``; the server refuses any handshake that does not present a
   valid client certificate — the same contract the HA stack enforces.
2. Config enforcement: the HA infra configs require mTLS on every prompt-listed
   service (Vault, Redis, Postgres, ClickHouse, OTel, nginx LBs, pgBouncer).
"""

from __future__ import annotations

import ssl
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from agent_obs.exporters.langfuse_exporter import LangfuseExporter
from agent_obs.observability import Span, SpanContext, SpanType, _now

REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# In-process PKI
# ---------------------------------------------------------------------------

def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _name(cn: str) -> x509.Name:
    return x509.Name(
        [
            x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OryaObservability"),
            x509.NameAttribute(NameOID.COMMON_NAME, cn),
        ]
    )


def _write_key(path: Path, key: rsa.RSAPrivateKey) -> None:
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )


def _write_cert(path: Path, cert: x509.Certificate) -> None:
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _ca_key_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=False,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=True,
        crl_sign=True,
        encipher_only=False,
        decipher_only=False,
    )


def _leaf_key_usage() -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=True,
        content_commitment=False,
        key_encipherment=True,
        data_encipherment=False,
        key_agreement=False,
        key_cert_sign=False,
        crl_sign=False,
        encipher_only=False,
        decipher_only=False,
    )


def _issue(
    ca_key: rsa.RSAPrivateKey,
    ca_cert: x509.Certificate,
    cn: str,
    sans: list[x509.GeneralName],
    not_valid_after: datetime,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = _key()
    now = datetime.now(timezone.utc)
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    issuer_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    return (
        x509.CertificateBuilder()
        .subject_name(_name(cn))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(not_valid_after)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None), critical=True
        )
        .add_extension(_leaf_key_usage(), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]
            ),
            critical=False,
        )
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(ski, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(
                issuer_ski
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    ), key


@dataclass
class Pki:
    ca_pem: Path
    server_pem: Path
    server_key: Path
    client_pem: Path
    client_key: Path
    expired_pem: Path
    expired_key: Path


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Pki:
    """Mint a private CA plus server / valid-client / expired-client certs."""
    d = tmp_path_factory.mktemp("mtls-pki")

    ca_key = _key()
    now = datetime.now(timezone.utc)
    ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("orya-test-internal-ca"))
        .issuer_name(_name("orya-test-internal-ca"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .add_extension(_ca_key_usage(), critical=True)
        .add_extension(ca_ski, critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = d / "ca.pem"
    _write_cert(ca_pem, ca_cert)

    server_cert, server_key = _issue(
        ca_key,
        ca_cert,
        cn="localhost",
        sans=[
            x509.DNSName("localhost"),
            x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1")),
        ],
        not_valid_after=now + timedelta(days=30),
    )
    server_pem, server_key_pem = d / "server.pem", d / "server-key.pem"
    _write_cert(server_pem, server_cert)
    _write_key(server_key_pem, server_key)

    client_cert, client_key = _issue(
        ca_key,
        ca_cert,
        cn="sdk",
        sans=[x509.DNSName("sdk")],
        not_valid_after=now + timedelta(days=30),
    )
    client_pem, client_key_pem = d / "client.pem", d / "client-key.pem"
    _write_cert(client_pem, client_cert)
    _write_key(client_key_pem, client_key)

    expired_cert, expired_key = _issue(
        ca_key,
        ca_cert,
        cn="sdk-expired",
        sans=[x509.DNSName("sdk-expired")],
        not_valid_after=now - timedelta(hours=1),
    )
    expired_pem, expired_key_pem = d / "expired.pem", d / "expired-key.pem"
    _write_cert(expired_pem, expired_cert)
    _write_key(expired_key_pem, expired_key)

    return Pki(
        ca_pem=ca_pem,
        server_pem=server_pem,
        server_key=server_key_pem,
        client_pem=client_pem,
        client_key=client_key_pem,
        expired_pem=expired_pem,
        expired_key=expired_key_pem,
    )


class _OtlpHandler(BaseHTTPRequestHandler):
    """Minimal OTLP/HTTP endpoint that requires a verified client cert."""

    hits: list[str | None] = []

    def do_POST(self) -> None:  # noqa: N802 (stdlib API)
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        _OtlpHandler.hits.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args: object) -> None:  # silence test noise
        return


@pytest.fixture
def mtls_endpoint(pki: Pki):
    """HTTPS server that only accepts clients presenting a CA-signed cert."""
    _OtlpHandler.hits = []
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(pki.server_pem), str(pki.server_key))
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(str(pki.ca_pem))

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _OtlpHandler)
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)

    # An aborted handshake (expired / missing client cert) raises inside the
    # per-connection thread; that abort IS the expected outcome of the negative
    # tests, so swallow it instead of failing the running test.
    httpd.handle_error = lambda request, client_address: None  # type: ignore[method-assign]

    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"https://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


def _make_span() -> Span:
    ctx = SpanContext.new(agent_id="mtls-test-agent", agent_version="0.1.0")
    return Span(
        name="llm.call:gpt-4o",
        span_type=SpanType.LLM_CALL,
        context=ctx,
        start_time=_now(),
        end_time=_now(),
        attributes={"status": "ok", "llm.model": "gpt-4o"},
    )


# ---------------------------------------------------------------------------
# SDK mTLS behaviour
# ---------------------------------------------------------------------------

class TestSdkMtls:
    async def test_export_succeeds_with_valid_client_cert(
        self, mtls_endpoint: str, pki: Pki
    ) -> None:
        exporter = LangfuseExporter(
            endpoint=mtls_endpoint,
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=1,
            tls_ca=str(pki.ca_pem),
            tls_cert=str(pki.client_pem),
            tls_key=str(pki.client_key),
        )
        try:
            await exporter.export([_make_span()])
            assert exporter._pending == []
            assert _OtlpHandler.hits, "server never received the OTLP POST"
        finally:
            await exporter.close()

    async def test_export_fails_without_ca(
        self, mtls_endpoint: str, pki: Pki
    ) -> None:
        # No AGENT_OBS_TLS_CA: the private CA is not in the system trust store,
        # so the server certificate cannot be verified (PP0 DoD: connection
        # refused without a valid CA).
        exporter = LangfuseExporter(
            endpoint=mtls_endpoint,
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=1,
            tls_cert=str(pki.client_pem),
            tls_key=str(pki.client_key),
        )
        try:
            await exporter.export([_make_span()])
            assert len(exporter._pending) == 1
            assert _OtlpHandler.hits == []
        finally:
            await exporter.close()

    async def test_export_fails_without_client_cert(
        self, mtls_endpoint: str, pki: Pki
    ) -> None:
        # CA is trusted but no client certificate is presented; the server
        # requires one (mTLS) and aborts the handshake.
        exporter = LangfuseExporter(
            endpoint=mtls_endpoint,
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=1,
            tls_ca=str(pki.ca_pem),
        )
        try:
            await exporter.export([_make_span()])
            assert len(exporter._pending) == 1
            assert _OtlpHandler.hits == []
        finally:
            await exporter.close()

    async def test_export_fails_with_expired_client_cert(
        self, mtls_endpoint: str, pki: Pki
    ) -> None:
        exporter = LangfuseExporter(
            endpoint=mtls_endpoint,
            public_key="pk-test",
            secret_key="sk-test",
            max_retries=1,
            tls_ca=str(pki.ca_pem),
            tls_cert=str(pki.expired_pem),
            tls_key=str(pki.expired_key),
        )
        try:
            await exporter.export([_make_span()])
            assert len(exporter._pending) == 1
            assert _OtlpHandler.hits == []
        finally:
            await exporter.close()

    async def test_from_env_reads_mtls_variables(self, pki: Pki) -> None:
        exporter = LangfuseExporter.from_env(
            {
                "LANGFUSE_HOST": "https://langfuse.example.com",
                "LANGFUSE_PUBLIC_KEY": "pk-env",
                "LANGFUSE_SECRET_KEY": "sk-env",
                "AGENT_OBS_TLS_CA": str(pki.ca_pem),
                "AGENT_OBS_TLS_CERT": str(pki.client_pem),
                "AGENT_OBS_TLS_KEY": str(pki.client_key),
            }
        )
        try:
            assert isinstance(exporter, LangfuseExporter)
            assert exporter._tls_ca == str(pki.ca_pem)
            assert exporter._tls_cert == str(pki.client_pem)
            assert exporter._tls_key == str(pki.client_key)
        finally:
            await exporter.close()

    async def test_from_env_without_mtls_variables_is_backward_compatible(
        self,
    ) -> None:
        exporter = LangfuseExporter.from_env(
            {
                "LANGFUSE_HOST": "https://langfuse.example.com",
                "LANGFUSE_PUBLIC_KEY": "pk-env",
                "LANGFUSE_SECRET_KEY": "sk-env",
            }
        )
        try:
            assert isinstance(exporter, LangfuseExporter)
            assert exporter._tls_cert is None
            assert exporter._tls_key is None
        finally:
            await exporter.close()

    def test_bundled_cert_file_needs_no_key(self, pki: Pki, tmp_path: Path) -> None:
        # A single PEM may bundle certificate + private key; tls_key optional.
        bundled = tmp_path / "bundled.pem"
        bundled.write_bytes(
            pki.client_pem.read_bytes() + pki.client_key.read_bytes()
        )

        async def _run() -> None:
            exporter = LangfuseExporter(
                endpoint="https://langfuse.example.com",
                public_key="pk",
                secret_key="sk",
                tls_ca=str(pki.ca_pem),
                tls_cert=str(bundled),
            )
            await exporter.close()

        import asyncio

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Infra config enforcement (Docker-free)
# ---------------------------------------------------------------------------

class TestInfraMtlsConfig:
    def test_compose_mounts_cert_volumes_for_core_services(self) -> None:
        text = (REPO_ROOT / "infra" / "docker-compose.ha.yml").read_text(
            encoding="utf-8"
        )
        for volume in (
            "certs_postgres",
            "certs_redis",
            "certs_vault",
            "certs_vault_unseal",
            "certs_clickhouse",
            "certs_nginx",
            "certs_otel",
            "certs_langfuse",
            "certs_sdk",
        ):
            assert f"{volume}:" in text, f"{volume} not declared in compose"
        assert "certs-init:" in text
        assert "./ha/clickhouse-tls.xml" in text
        assert "pg_hba_mtls.conf" in text
        assert "redis-tls.conf" in text

    def test_compose_postgres_enables_ssl_and_cert_hba(self) -> None:
        text = (REPO_ROOT / "infra" / "docker-compose.ha.yml").read_text(
            encoding="utf-8"
        )
        assert "ssl=on" in text
        assert "ssl_cert_file=/var/lib/postgresql/certs/postgres.pem" in text
        assert "hba_file=/etc/postgresql/pg_hba.conf" in text

    def test_compose_vault_listener_requires_client_certs(self) -> None:
        text = (REPO_ROOT / "infra" / "docker-compose.ha.yml").read_text(
            encoding="utf-8"
        )
        assert "tls_disable_client_certs\\\":false" in text or (
            '"tls_disable_client_certs":false' in text
        )
        assert "https://localhost:8200" in text

    def test_pg_hba_requires_verified_client_certs(self) -> None:
        text = (
            REPO_ROOT / "infra" / "ha" / "postgres" / "pg_hba_mtls.conf"
        ).read_text(encoding="utf-8")
        assert "hostssl" in text
        assert "clientcert=verify-full" in text

    def test_redis_tls_requires_client_certs(self) -> None:
        text = (REPO_ROOT / "infra" / "ha" / "redis" / "redis-tls.conf").read_text(
            encoding="utf-8"
        )
        assert "tls-port 6380" in text
        assert "tls-auth-clients require" in text

    def test_vault_raft_config_enforces_mtls(self) -> None:
        text = (
            REPO_ROOT / "infra" / "ha" / "vault" / "vault-raft.hcl"
        ).read_text(encoding="utf-8")
        assert "tls_cert_file" in text
        assert "tls_ca_cert_file" in text
        assert "tls_disable_client_certs  = false" in text.replace(
            "tls_disable_client_certs = false",
            "tls_disable_client_certs  = false",
        ) or "tls_disable_client_certs = false" in text

    def test_clickhouse_tls_requires_verified_clients(self) -> None:
        text = (
            REPO_ROOT / "infra" / "ha" / "clickhouse-tls.xml"
        ).read_text(encoding="utf-8")
        assert "<https_port>8443</https_port>" in text
        assert "<verificationMode>strict</verificationMode>" in text

    def test_nginx_load_balancers_require_client_certs(self) -> None:
        for name in ("nginx-langfuse.conf", "nginx-otel.conf"):
            text = (REPO_ROOT / "infra" / "ha" / "nginx" / name).read_text(
                encoding="utf-8"
            )
            assert "ssl_verify_client on" in text, name
            assert "ssl_client_certificate" in text, name

    def test_otel_collector_config_verifies_client_certs(self) -> None:
        text = (
            REPO_ROOT / "infra" / "ha" / "otel-tls-config.yaml"
        ).read_text(encoding="utf-8")
        assert text.count("client_ca_file:") >= 2  # grpc + http receivers
        assert "https://nginx-langfuse:3000" in text

    def test_pgbouncer_enables_tls_both_ways(self) -> None:
        text = (
            REPO_ROOT / "infra" / "ha" / "postgres" / "pgbouncer.ini"
        ).read_text(encoding="utf-8")
        assert "client_tls_sslmode = verify-ca" in text
        assert "server_tls_sslmode = verify-full" in text

    def test_cert_material_is_gitignored(self) -> None:
        text = (REPO_ROOT / "infra" / "ha" / "certs" / ".gitignore").read_text(
            encoding="utf-8"
        )
        # Whitelist style: everything is ignored except .gitignore + README
        assert "\n*\n" in text or text.startswith("*")
        assert "!.gitignore" in text

    def test_cert_lifecycle_scripts_exist(self) -> None:
        for rel in (
            "scripts/certs/generate-ca.sh",
            "scripts/certs/issue-certs.sh",
            "scripts/rotate-certs.sh",
            "infra/ha/certs-init.sh",
            "infra/ha/certs/README.md",
        ):
            assert (REPO_ROOT / rel).exists(), f"missing {rel}"
