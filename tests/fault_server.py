"""Manual browser fixture: force every live data request to fail.

Run from the project root with ``python tests\fault_server.py`` and open
http://127.0.0.1:8766.  The normal disk cache is left intact, so the page must
label cached quotes and history explicitly instead of presenting them as live.
"""

import sys
from pathlib import Path
from http.server import ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import server  # noqa: E402
from providers import DataSourceError, MarketProvider  # noqa: E402


class FailingProvider(MarketProvider):
    name = "fault-test"

    def quotes(self, symbols):
        raise DataSourceError("故障演练：所有实时行情源均不可用")

    def history(self, symbol, limit=250):
        raise DataSourceError("故障演练：所有实时历史源均不可用")


server.get_provider = lambda preference="auto": FailingProvider()


if __name__ == "__main__":
    httpd = ThreadingHTTPServer(("127.0.0.1", 8766), server.Handler)
    print("缓存降级故障演练：http://127.0.0.1:8766", flush=True)
    httpd.serve_forever()
