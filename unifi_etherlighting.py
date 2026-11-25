#!/usr/bin/env python3
"""Set Etherlighting brightness on a UniFi OS console without a browser."""

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import ssl
import sys
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from http.client import HTTPException, HTTPSConnection
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import quote, urlsplit
from urllib.request import Request
from zoneinfo import ZoneInfo


class UniFiError(Exception):
    pass


class SessionExpired(UniFiError):
    pass


class WriteUnconfirmed(UniFiError):
    pass


@dataclass(frozen=True)
class DeviceLighting:
    device_id: str
    name: str
    settings: dict

    @property
    def brightness(self):
        return self.settings["brightness"]


def csrf_from_cookie(token):
    try:
        encoded = token.split(".")[1]
        claims = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        )
    except (IndexError, ValueError):
        return None
    if isinstance(claims, dict) and isinstance(claims.get("csrfToken"), str):
        return claims["csrfToken"]
    return None


class UniFiClient:
    def __init__(self, console, site, username, password, cert_sha256):
        self.console = console
        self.site_path = f"/proxy/network/api/s/{quote(site, safe='')}"
        self.username = username
        self.password = password
        self.cert_sha256 = cert_sha256
        self.context = ssl.create_default_context()
        if cert_sha256:
            # A supplied fingerprint authenticates the leaf certificate before any HTTP data is sent.
            self.context.check_hostname = False
            self.context.verify_mode = ssl.CERT_NONE
        self.cookies = CookieJar()
        self.csrf = None
        self.renewed = False

    def session_cookie(self):
        return next(
            (cookie.value for cookie in self.cookies if cookie.name == "TOKEN"), ""
        )

    def request(self, method, path, payload=None):
        headers = {"Accept": "application/json"}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        if method == "PUT":
            if not self.csrf:
                raise UniFiError(
                    "The UniFi session did not provide a CSRF token for the write."
                )
            headers["X-CSRF-Token"] = self.csrf

        request = Request(
            self.console.geturl().rstrip("/") + path, headers=headers, method=method
        )
        self.cookies.add_cookie_header(request)
        old_cookie = self.session_cookie()
        connection = HTTPSConnection(
            self.console.hostname, self.console.port, context=self.context, timeout=10
        )
        with closing(connection):
            connection.connect()
            if self.cert_sha256:
                cert = connection.sock.getpeercert(binary_form=True)
                actual = hashlib.sha256(cert).hexdigest()
                if not hmac.compare_digest(actual, self.cert_sha256):
                    raise UniFiError(
                        "The console certificate does not match the supplied SHA-256 fingerprint."
                    )
            connection.request(
                method, path, body=body, headers=dict(request.header_items())
            )
            with connection.getresponse() as response:
                status = response.status
                raw = response.read()
                self.cookies.extract_cookies(response, request)
                header_csrf = response.headers.get(
                    "X-Updated-CSRF-Token"
                ) or response.headers.get("X-CSRF-Token")

        new_cookie = self.session_cookie()
        if header_csrf or new_cookie != old_cookie:
            self.csrf = header_csrf or csrf_from_cookie(new_cookie)

        operation = f"{method} {path}"
        if path == "/api/auth/login" and status == 499:
            raise UniFiError(
                "UniFi requested MFA (HTTP 499). Use a local console account for unattended login."
            )
        if status == 401 or (status == 403 and b"csrf" in raw.lower()):
            raise SessionExpired(
                f"{operation} rejected authentication (HTTP {status})."
            )
        if not 200 <= status < 300:
            error = (
                WriteUnconfirmed if method == "PUT" and status >= 500 else UniFiError
            )
            raise error(f"{operation} returned HTTP {status}.")

        try:
            result = json.loads(raw) if raw else {}
        except ValueError as exc:
            error = WriteUnconfirmed if method == "PUT" else UniFiError
            raise error(f"{operation} did not return valid JSON.") from exc
        if not isinstance(result, dict):
            error = WriteUnconfirmed if method == "PUT" else UniFiError
            raise error(f"{operation} returned an unexpected JSON shape.")
        meta = result.get("meta")
        if isinstance(meta, dict) and meta.get("rc") == "error":
            if meta.get("msg") in ("api.err.LoginRequired", "api.err.InvalidSession"):
                raise SessionExpired(f"{operation} requires a fresh session.")
            raise UniFiError(f"{operation} was rejected by UniFi Network.")
        return result

    def login(self):
        self.cookies.clear()
        self.csrf = None
        self.request(
            "POST",
            "/api/auth/login",
            {"username": self.username, "password": self.password},
        )
        if not self.session_cookie():
            raise UniFiError(
                "Login did not return a UniFi OS session cookie. Check that this is a local console account."
            )

    def renew_session(self):
        if self.renewed:
            raise UniFiError("UniFi rejected the session after one fresh login.")
        self.renewed = True
        self.login()

    def read_lighting(self, selector, device_id=None):
        path = self.site_path + "/stat/device"
        try:
            result = self.request("GET", path)
        except SessionExpired:
            self.renew_session()
            result = self.request("GET", path)
        devices = result.get("data")
        if not isinstance(devices, list):
            raise UniFiError("The console did not return a device list.")

        def matches(device):
            if device_id is not None:
                return device.get("_id") == device_id
            mac = device.get("mac")
            return selector == device.get("name") or (
                isinstance(mac, str) and selector.lower() == mac.lower()
            )

        found = [
            device for device in devices if isinstance(device, dict) and matches(device)
        ]
        if len(found) != 1:
            raise UniFiError(
                f"Expected one device matching {selector!r}, found {len(found)}. Use its MAC address if names are duplicated."
            )
        device = found[0]
        settings = device.get("ether_lighting")
        if (
            not isinstance(settings, dict)
            or type(settings.get("brightness")) is not int
        ):
            raise UniFiError(
                "The selected device did not report a numeric Etherlighting brightness."
            )
        if not isinstance(device.get("_id"), str) or not device["_id"]:
            raise UniFiError("The selected device has no UniFi device ID.")
        return DeviceLighting(device["_id"], device.get("name") or selector, settings)


def set_brightness(client, selector, brightness, dry_run):
    current = client.read_lighting(selector)
    if dry_run:
        print(
            f"{current.name}: {current.brightness}% now, {brightness}% requested (dry run)"
        )
        return

    while current.brightness != brightness:
        path = client.site_path + "/rest/device/" + quote(current.device_id, safe="")
        payload = {"ether_lighting": {**current.settings, "brightness": brightness}}
        try:
            client.request("PUT", path, payload)
        except SessionExpired:
            client.renew_session()
            current = client.read_lighting(selector, current.device_id)
            continue
        except (OSError, HTTPException, WriteUnconfirmed) as exc:
            print(
                f"Write response uncertain ({type(exc).__name__}). Checking the saved setting.",
                file=sys.stderr,
            )

        # A lost response can still leave a successful update. Verify before reporting the result.
        for attempt in range(5):
            if attempt:
                time.sleep(2)
            saved = client.read_lighting(selector, current.device_id)
            if saved.brightness == brightness:
                print(
                    f"{saved.name}: {current.brightness}% to {saved.brightness}% (verified)"
                )
                return
        raise UniFiError(
            f"Could not verify {brightness}%. The console last reported {saved.brightness}%."
        )

    print(f"{current.name}: already at {brightness}% (verified)")


def console_url(value):
    try:
        url = urlsplit(value)
        port = url.port
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Enter a valid HTTPS console origin.") from exc
    if (
        url.scheme != "https"
        or not url.hostname
        or url.username is not None
        or port == 0
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise argparse.ArgumentTypeError(
            "Enter an HTTPS console origin, such as https://unifi.example.net."
        )
    return url


def certificate_fingerprint(value):
    value = value.replace(":", "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise argparse.ArgumentTypeError(
            "Use the console certificate's SHA-256 fingerprint (64 hex digits)."
        )
    return value


def scheduled_brightness(now):
    local_time = now.astimezone(ZoneInfo("America/Chicago"))
    return 100 if 7 <= local_time.hour < 23 else 1


def credential(name):
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        return (Path(directory) / name).read_text(encoding="utf-8").rstrip("\r\n")
    return os.environ.get(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--console",
        type=console_url,
        default=os.environ.get("UNIFI_CONSOLE"),
        help="Local HTTPS console origin, or set UNIFI_CONSOLE",
    )
    parser.add_argument(
        "--site",
        default=os.environ.get("UNIFI_SITE", "default"),
        help="UniFi Network site slug, or set UNIFI_SITE (default: default)",
    )
    parser.add_argument(
        "--device",
        default=os.environ.get("UNIFI_DEVICE"),
        help="Exact device name or colon-separated MAC address, or set UNIFI_DEVICE",
    )
    level = parser.add_mutually_exclusive_group()
    level.add_argument(
        "--brightness",
        type=int,
        default=100,
        metavar="1-100",
        help="Brightness percentage (default: 100)",
    )
    level.add_argument(
        "--daily-schedule",
        action="store_true",
        help="Use 100%% from 7 AM to 11 PM Central, and 1%% overnight",
    )
    parser.add_argument(
        "--username", help="Local console username, or set UNIFI_USERNAME"
    )
    parser.add_argument(
        "--cert-sha256",
        type=certificate_fingerprint,
        default=os.environ.get("UNIFI_CERT_SHA256") or None,
        help="Verified certificate fingerprint, or set UNIFI_CERT_SHA256",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Sign in and read the current brightness without writing",
    )
    args = parser.parse_args()
    if not args.console:
        parser.error("Provide --console or set UNIFI_CONSOLE.")
    if not args.device:
        parser.error("Provide --device or set UNIFI_DEVICE.")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.site):
        parser.error("--site must be a UniFi Network site slug.")
    if not 1 <= args.brightness <= 100:
        parser.error("--brightness must be between 1 and 100.")

    try:
        username = args.username or credential("UNIFI_USERNAME")
        if not username:
            parser.error("Provide --username or set UNIFI_USERNAME.")
        password = credential("UNIFI_PASSWORD")
        if not password:
            if not sys.stdin.isatty():
                parser.error("Set UNIFI_PASSWORD for a non-interactive run.")
            password = getpass.getpass("Local UniFi password: ")
        brightness = (
            scheduled_brightness(datetime.now().astimezone())
            if args.daily_schedule
            else args.brightness
        )
        client = UniFiClient(
            args.console, args.site, username, password, args.cert_sha256
        )
        client.login()
        set_brightness(client, args.device, brightness, args.dry_run)
    except (UniFiError, OSError, HTTPException) as exc:
        print(f"UniFi: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
