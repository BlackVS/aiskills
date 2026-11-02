"""A fake Git credential helper for the tests, set through credential.helper. It answers `get`
for the one host its environment names, with a fake token, and appends each query it reads to
the file FAKE_CREDENTIAL_LOG names. It never holds a real credential."""
import json, os, sys

if sys.argv[-1] != "get":
    sys.exit(0)  # store and erase: nothing to keep
query = dict(line.partition("=")[::2] for line in sys.stdin.read().splitlines() if line)
with open(os.environ["FAKE_CREDENTIAL_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(query) + "\n")
user = os.environ.get("FAKE_CREDENTIAL_USER", "reader")
if query.get("host") == os.environ.get("FAKE_CREDENTIAL_HOST") and query.get("username", user) == user:
    sys.stdout.write(f"username={user}\npassword={os.environ['FAKE_CREDENTIAL_TOKEN']}\n")
