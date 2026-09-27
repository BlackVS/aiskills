"""Pure helpers of .github/check-clients.py (the client runs themselves need npm and network,
so they run in the Clients workflow, not here).

Run: python3 -m unittest tests/test_check_clients.py
"""
import importlib.util, unittest, urllib.parse

from tests.test_install import ROOT

spec = importlib.util.spec_from_file_location("check_clients", ROOT / ".github" / "check-clients.py")
check_clients = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check_clients)


class CatalogUrl(unittest.TestCase):
    def test_directory_survives_spaces_and_query_characters(self):
        directory = "/tmp/client checks & more/#1?x=y"
        url = check_clients.catalog_url(47401, "/api/skill", directory)
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.path, "/api/skill")
        self.assertNotIn(" ", url)
        self.assertEqual(urllib.parse.parse_qs(parts.query), {"location[directory]": [directory]})


if __name__ == "__main__":
    unittest.main()
