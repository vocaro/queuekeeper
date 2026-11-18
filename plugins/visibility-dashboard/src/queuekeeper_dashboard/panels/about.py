"""The About page: which Queuekeeper this dashboard is, its license, and the notices of the work of others it uses,
each with the credit and the license that work asks for. It has no tab: every page links to it from its header.

NOTICES is the page's list; the plugin's THIRD_PARTY_NOTICES.md says the same at length, file by file, and a test
holds the two to the same works, so a notice added to one is added to the other."""
from queuekeeper import __version__, restartable

TITLE, ORDER = "About", 1000
TAB = False
LICENSE = {"name": "Apache License 2.0", "url": "https://www.apache.org/licenses/LICENSE-2.0"}
COPYRIGHT = "Copyright 2026 Vocaro Technologies LLC"
# Each work of others the dashboard uses: what it is, where it is used, whose it is, and its license.
NOTICES = [
    {"name": "Streamline Ultimate Colors icons", "used": "The Farm tab's device icons, used unchanged",
     "by": "Streamline", "url": "https://streamlinehq.com",
     "license": "CC BY 4.0", "licenseUrl": "https://creativecommons.org/licenses/by/4.0/"},
]


def answer():
    checkout = restartable.code_checkout()
    return {"version": __version__, "commit": restartable.head(checkout) if checkout else None,
            "license": LICENSE, "copyright": COPYRIGHT, "notices": NOTICES}


def data(repository, query):
    return answer()


def everywhere(repositories, query):
    return answer()
