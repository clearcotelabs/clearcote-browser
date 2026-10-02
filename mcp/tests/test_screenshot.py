"""screenshot_page must not change the page's DOM.

Playwright's page.screenshot() first sets an inline caret-color on every input, textarea and contenteditable (and
restores it afterwards): changes a page script can observe. The facade captures through raw CDP instead.

The first test runs anywhere (fakes, no browser). The second drives a real Clearcote browser and is opt-in:
CLEARCOTE_MCP_BROWSER_TESTS=1.
"""
import base64
import os
import struct

import pytest

from clearcote_mcp._facade import ClearcoteBrowser

PNG = base64.b64decode(  # a 1x1 PNG
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


class FakeCDP:
    def __init__(self):
        self.sent, self.detached = [], False

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method == "Page.getLayoutMetrics":
            return {"cssContentSize": {"x": 0, "y": 0, "width": 800, "height": 3200}}
        return {"data": base64.b64encode(PNG).decode()}

    async def detach(self):
        self.detached = True


class FakePage:
    url = "https://example.com/"

    async def screenshot(self, **kwargs):
        raise AssertionError("page.screenshot() changes the page's DOM; capture through CDP")


class FakeContext:
    def __init__(self):
        self.cdp = FakeCDP()

    async def new_cdp_session(self, page):
        return self.cdp


@pytest.mark.asyncio
@pytest.mark.parametrize("full_page", [True, False])
async def test_screenshot_captures_through_cdp(tmp_path, full_page):
    b = ClearcoteBrowser()
    b._ctx, b._page = FakeContext(), FakePage()
    out = await b.screenshot(str(tmp_path / "s.png"), full_page=full_page)
    assert out == {"status": "ok", "url": FakePage.url, "path": str(tmp_path / "s.png")}
    assert (tmp_path / "s.png").read_bytes() == PNG
    method, params = b._ctx.cdp.sent[-1]
    assert method == "Page.captureScreenshot" and b._ctx.cdp.detached
    if full_page:  # the whole document, not the viewport
        assert params["captureBeyondViewport"] is True
        assert params["clip"] == {"x": 0, "y": 0, "width": 800, "height": 3200, "scale": 1}
    else:
        assert "clip" not in params


PAGE = """<!doctype html><html><head><title>t</title></head><body style="margin:0">
<input value="x"><textarea></textarea><div contenteditable="true">edit</div>
<div style="height:3000px">tall</div>
<script>
window.muts = 0;
new MutationObserver(list => { window.muts += list.length; })
  .observe(document, {subtree: true, attributes: true, childList: true, characterData: true});
</script></body></html>"""


def png_size(path):
    with open(path, "rb") as f:
        return struct.unpack(">II", f.read(24)[16:24])


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("CLEARCOTE_MCP_BROWSER_TESTS") != "1",
                    reason="drives a real Clearcote browser; set CLEARCOTE_MCP_BROWSER_TESTS=1")
async def test_screenshot_leaves_the_dom_untouched_in_a_real_browser(tmp_path):
    b = ClearcoteBrowser({"headless": True})
    await b.start()
    try:
        page = tmp_path / "p.html"
        page.write_text(PAGE)
        await b.navigate(page.as_uri())
        for full_page in (True, False):
            shot = tmp_path / f"s-{full_page}.png"
            await b.screenshot(str(shot), full_page=full_page)
            width, height = png_size(shot)
            inner = await b._page.evaluate("[innerWidth, innerHeight, devicePixelRatio]")
            if full_page:
                assert height >= 3000 * inner[2]  # the whole tall document
            else:
                assert height == round(inner[1] * inner[2])  # just the viewport
        assert await b._page.evaluate("window.muts") == 0  # nothing the page could observe
    finally:
        await b.close()
