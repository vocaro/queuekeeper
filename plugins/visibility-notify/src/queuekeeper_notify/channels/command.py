"""A command of the owner's, given the notice as JSON on its standard input. Its `argv` is an argument list, never
shell source; the notice's title and body follow it as two more arguments."""
import json
import subprocess

LIMIT = 30


def ready(config):
    argv = config.get("argv")
    if not (isinstance(argv, list) and argv and all(isinstance(word, str) for word in argv)):
        return "no argv is set: `qk notify set command argv '[\"PROGRAM\", \"ARGUMENT\"]'`"
    return None


def send(notice, config):
    done = subprocess.run([*config["argv"], notice["title"], notice["body"]], input=json.dumps(notice), text=True,
                          capture_output=True, timeout=LIMIT)
    if done.returncode: raise RuntimeError((done.stderr.strip() or done.stdout.strip() or "no output")[-300:]
                                           + f" (exit {done.returncode})")
