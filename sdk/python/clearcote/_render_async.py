"""Async counterpart of :func:`clearcote._render.check_render_coherence` (#7).

Shares the probe JS and the pure analysis (:func:`evaluate_render_info`) with the sync module; only
the ``page.evaluate`` await differs.
"""

from ._render import PROBE_JS, evaluate_render_info


async def check_render_coherence(page, claimed_gpu=None):
    """Async render-backend coherence probe. See :func:`clearcote._render.check_render_coherence`.

    Example::

        br = await clearcote.async_api.launch(fingerprint="77")
        page = await br.new_page(); await page.goto("about:blank")
        verdict = await clearcote.async_api.check_render_coherence(page)
        assert verdict["coherent"], verdict["warnings"]
    """
    info = await page.evaluate(PROBE_JS)
    return evaluate_render_info(info, claimed_gpu)
