"""Self-signed serving certificates for the Kind harness and the tests.

OpenShift's service CA normally issues these from a
`service.beta.openshift.io/serving-cert-secret-name` annotation, and fills in the
`service-ca.crt` key of an annotated ConfigMap. Kind has neither, so the harness
and the EvalHub fixtures mint their own certificate and use it both as the served
certificate and as the CA bundle that verifies it.
"""

from __future__ import annotations

import datetime

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

# Fixed, and short: a common name built from the first DNS name plus a namespace
# can exceed the 64-character limit x509 puts on attribute values.
COMMON_NAME = "trustyai-serving-cert"


def generate_serving_cert(dns_names: list[str], days: int = 365) -> tuple[bytes, bytes]:
    """Return a self-signed certificate and its private key, both PEM encoded."""
    if not dns_names:
        raise ValueError("dns_names must not be empty")

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, COMMON_NAME)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(name) for name in dns_names]),
            critical=False,
        )
        # Self-signed: it is its own CA, so the same bundle verifies it.
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
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
    key_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return certificate.public_bytes(serialization.Encoding.PEM), key_pem


def service_dns_names(service: str, namespace: str) -> list[str]:
    """The names a Service is reached by inside the cluster."""
    return [
        f"{service}.{namespace}.svc",
        f"{service}.{namespace}.svc.cluster.local",
    ]
