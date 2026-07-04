"""
authorize.py
------------
A tiny standalone helper to complete the OAuth login and sanity-check your
credentials WITHOUT the web server. Handy for first-time setup or for debugging
the "redirect URL does not match" error in isolation.

Usage:
    python authorize.py

It opens your browser, captures the redirect on the port in your redirect URI,
exchanges the code for tokens (saved to tokens.json), then lists your locations
and thermostats so you can confirm everything works end to end.
"""

from __future__ import annotations

import html
import logging
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from config import Config
from honeywell_client import HoneywellClient

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = parse_qs(urlparse(self.path).query)
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        if "code" in q:
            self.server.auth_code = q["code"][0]
            self.server.got_redirect = True
            self.wfile.write(b"<h3>Authorized. You can close this tab and return to the terminal.</h3>")
        elif "error" in q:
            err = q["error"][0]
            self.server.auth_error = err
            self.server.got_redirect = True
            # Escape the (attacker-influenceable) error value before reflecting it.
            self.wfile.write(f"<h3>Authorization failed: {html.escape(err)}</h3>".encode())
        else:
            # Not the OAuth redirect (favicon, port scanner, health check, ...).
            # Don't consume our single-use auth code on it - keep waiting.
            self.wfile.write(b"<h3>Waiting for the OAuth redirect...</h3>")

    def log_message(self, *a):
        return


def main():
    Config.require_credentials()
    client = HoneywellClient(
        api_key=Config.API_KEY,
        api_secret=Config.API_SECRET,
        redirect_uri=Config.REDIRECT_URI,
    )

    url = client.authorize_url(state="cli")
    parsed = urlparse(Config.REDIRECT_URI)
    port = parsed.port or 80

    print("\nRedirect URI configured as:", Config.REDIRECT_URI)
    print("Make sure that EXACT string is registered on the developer portal.\n")
    print("Opening browser to authorize... if it doesn't open, visit:\n", url, "\n")
    webbrowser.open(url)

    # Bind loopback only: the auth code is single-use, so anything else on the
    # network hitting this port first could eat it (or feed us a bogus ?error=).
    httpd = HTTPServer(("127.0.0.1", port), _Handler)
    httpd.timeout = 5  # seconds per handle_request(), so we can re-check the deadline
    httpd.auth_code = None
    httpd.auth_error = None
    httpd.got_redirect = False

    print(f"Waiting for the redirect on 127.0.0.1:{port} ...")
    # Loop until the genuine redirect (carrying ?code= or ?error=) arrives instead
    # of accepting the first arbitrary GET, but give up after a bounded wait.
    deadline = time.monotonic() + 300
    while not httpd.got_redirect and time.monotonic() < deadline:
        httpd.handle_request()

    if not httpd.got_redirect:
        print("Timed out waiting for the OAuth redirect. Check the redirect URI match and try again.")
        return
    if httpd.auth_error:
        print(f"Authorization failed: {httpd.auth_error}")
        return
    if not httpd.auth_code:
        print("No authorization code received. Check the redirect URI match and try again.")
        return

    print("Got authorization code; exchanging for tokens...")
    client.exchange_code(httpd.auth_code)
    print("Tokens saved to tokens.json\n")

    print("Fetching locations and thermostats...\n")
    for loc in client.get_locations():
        print(f"Location: {loc.get('name')} (ID {loc.get('locationID')})")
        for t in client.get_thermostats(loc.get("locationID")):
            cv = t.get("changeableValues", {})
            print(f"  - {t.get('userDefinedDeviceName') or t.get('name')} "
                  f"[{t.get('deviceID')}] online={t.get('isAlive')} "
                  f"temp={t.get('indoorTemperature')} mode={cv.get('mode')} "
                  f"heat={cv.get('heatSetpoint')} cool={cv.get('coolSetpoint')}")
    print("\nAll good. Start the dashboard with:  python app.py")


if __name__ == "__main__":
    main()
