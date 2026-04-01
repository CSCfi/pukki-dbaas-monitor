#!/usr/bin/env python3
import json
import os
import smtplib
import subprocess
import sys
from datetime import datetime, timezone
from email.message import EmailMessage


THRESHOLD_PERCENT = float(os.environ.get("THRESHOLD_PERCENT", "90"))

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.pouta.csc.fi")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "25"))
MAIL_FROM = os.environ["MAIL_FROM"]
MAIL_TO = os.environ["MAIL_TO"]


def log(msg: str) -> None:
    print(f"{datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


def send_email(subject: str, body: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = MAIL_FROM
    msg["To"] = MAIL_TO
    msg["Sender"] = MAIL_FROM
    msg.set_content(body)

    recipients = [addr.strip() for addr in MAIL_TO.split(",") if addr.strip()]
    if not recipients:
        raise RuntimeError("MAIL_TO does not contain any valid recipient addresses")

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.sendmail(MAIL_FROM, recipients, msg.as_string())


def run_openstack_command(args: list[str]) -> str:
    result = subprocess.run(
        args,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(args)}\n"
            f"exit_code={result.returncode}\n"
            f"stdout={result.stdout.strip()}\n"
            f"stderr={result.stderr.strip()}"
        )

    return result.stdout


def list_instance_names() -> list[str]:
    output = run_openstack_command(
        ["openstack", "database", "instance", "list", "-f", "json"]
    )
    rows = json.loads(output)

    names = []
    for row in rows:
        name = row.get("Name")
        if name:
            names.append(name)

    return sorted(names)


def show_instance(instance_name: str) -> dict:
    output = run_openstack_command(
        ["openstack", "database", "instance", "show", instance_name, "-f", "json"]
    )
    return json.loads(output)


def to_float(value) -> float:
    if value is None:
        raise ValueError("value is None")
    return float(str(value).strip())


def main() -> int:
    failures = []
    alerts = []

    log("Starting OpenStack DBaaS volume usage check")

    try:
        instance_names = list_instance_names()
    except Exception as e:
        log(f"ERROR listing instances: {e}")
        return 1

    if not instance_names:
        log("No database instances found")
        return 0

    log(f"Found {len(instance_names)} instance(s): {', '.join(instance_names)}")

    for name in instance_names:
        try:
            data = show_instance(name)

            volume_gb = to_float(data["volume"])
            used_gb = to_float(data["volume_used"])

            if volume_gb <= 0:
                raise ValueError(f"invalid volume value: {volume_gb}")

            used_pct = (used_gb / volume_gb) * 100.0

            status = data.get("status", "UNKNOWN")
            operating_status = data.get("operating_status", "UNKNOWN")

            log(
                f"{name}: {used_gb:.2f} GB / {volume_gb:.2f} GB = {used_pct:.2f}% "
                f"(status={status}, operating_status={operating_status})"
            )

            if status != "ACTIVE":
                log(f"Skipping alert evaluation for {name} because status is {status}")
                continue

            if used_pct >= THRESHOLD_PERCENT:
                alerts.append({
                    "name": name,
                    "used_gb": used_gb,
                    "volume_gb": volume_gb,
                    "used_pct": used_pct,
                    "status": status,
                    "operating_status": operating_status,
                })

        except Exception as e:
            failures.append(f"{name}: {e}")
            log(f"ERROR {name}: {e}")

    if alerts:
        lines = [
            f"These DB instances are above {THRESHOLD_PERCENT:.0f}% volume usage:",
            ""
        ]

        for a in alerts:
            lines.append(
                f"- {a['name']}: {a['used_gb']:.2f} GB / {a['volume_gb']:.2f} GB "
                f"= {a['used_pct']:.2f}% "
                f"(status={a['status']}, operating_status={a['operating_status']})"
            )

        send_email(
            subject=f"[KUHA DB ALERT] DB instances above {THRESHOLD_PERCENT:.0f}%",
            body="\n".join(lines),
        )
        log(f"Alert email sent for {len(alerts)} instance(s)")
    else:
        log(f"No DB instances above {THRESHOLD_PERCENT:.0f}%")

    if failures:
        log("Some instance checks failed:")
        for f in failures:
            log(f"  - {f}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())