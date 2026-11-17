#!/usr/bin/env python3
"""Generate a self-signed serving certificate for the TrustyAI services.

OpenShift's service CA normally issues the webhook and metrics certificates from
`service.beta.openshift.io/serving-cert-secret-name` annotations. Kind has no
service CA, so the harness creates those secrets itself from this certificate and
reuses it as the CA bundle of the CRD conversion webhooks.
"""

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from utilities.certs import generate_serving_cert, service_dns_names  # noqa: E402

SERVICES = (
    "trustyai-service-operator-webhook-service",
    "trustyai-service-operator-metrics-service",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="system")
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--days", type=int, default=365)
    parser.add_argument(
        "--service",
        action="append",
        default=[],
        metavar="NAME",
        help="extra Service whose DNS names the certificate must cover, e.g. evalhub",
    )
    args = parser.parse_args()
    if not args.out_dir:
        parser.error("--out-dir must not be empty")

    dns_names = [
        dns_name
        for service in (*SERVICES, *args.service)
        for dns_name in service_dns_names(service=service, namespace=args.namespace)
    ]
    cert_pem, key_pem = generate_serving_cert(dns_names=dns_names, days=args.days)

    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "tls.crt").write_bytes(cert_pem)
    key_file = out_dir / "tls.key"
    key_file.write_bytes(key_pem)
    key_file.chmod(0o600)


if __name__ == "__main__":
    main()
