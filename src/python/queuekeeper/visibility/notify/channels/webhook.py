"""An HTTP POST of the notice as JSON to the owner's `url`, with any `headers`. The object has a `text` key, the
title and body in one line, which Slack-compatible webhooks post as they are."""
import json
import urllib.request
from urllib.parse import urlsplit

LIMIT = 10


def ready(config):
    url = config.get("url")
    if not (isinstance(url, str) and urlsplit(url).scheme in ("http", "https") and urlsplit(url).netloc):
        return "no http or https url is set: `queuekeeper notify set webhook url URL`"
    headers = config.get("headers", {})
    if not (isinstance(headers, dict) and all(isinstance(value, str) for value in headers.values())):
        return "headers must map each header's name to its text"
    return None


def send(notice, config):
    body = json.dumps({"text": f"{notice['repository']}: {notice['title']}. {notice['body']}", **notice}).encode()
    request = urllib.request.Request(config["url"], data=body, method="POST",
                                     headers={"Content-Type": "application/json", **config.get("headers", {})})
    with urllib.request.urlopen(request, timeout=LIMIT) as response:  # an HTTP error status raises
        response.read(1024)
