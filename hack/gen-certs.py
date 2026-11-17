#!/usr/bin/env python3
"""Generate a self-signed serving certificate for the TrustyAI services.

OpenShift's service CA normally issues the webhook and metrics certificates
from `service.beta.openshift.io/serving-cert-secret-name` annotations. Kind has
no service CA, so the harness creates those secrets itself from this
certificate and reuses it as the CA bundle of the CRD conversion webhooks.
"""

import argparse
import datetime
import pathlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

SERVICES = (
    "trustyai-service-operator-webhook-service",
    "trustyai-service-operator-metrics-service",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="system")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--days", type=int, default=365)
    args = parser.parse_args()
    if not args.out_dir:
        parser.error("--out-dir must not be empty")

    dns_names = [
        f"{service}.{args.namespace}.svc" for service in SERVICES
    ] + [f"{service}.{args.namespace}.svc.cluster.local" for service in SERVICES]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, dns_names[0])])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=args.days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(dns) for dns in dns_names]),
            critical=False,
        )
        # Self-signed: it is its own CA, so the same bundle verifies the served
        # certificate of both services.
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None), critical=True
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_encipherment=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tls.crt").write_bytes(
        certificate.public_bytes(serialization.Encoding.PEM)
    )
    key_file = out_dir / "tls.key"
    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)


if __name__ == "__main__":
    main()
