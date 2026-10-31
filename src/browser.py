"""browser.py — the hands. Playwright, imported only when actually used.

The rest of ai_Pobisk runs on the stdlib. A browser cannot: driving Chromium
means a protocol client, and writing one would be a second project. So this is
the single place with an optional dependency, and it is imported lazily —
`./ai_Pobisk.sh list` still works on a machine with no Playwright installed.

The contract with pageview.py is the `data-pb-ref` attribute. Before every
snapshot the driver wipes old refs and re-tags the live DOM in document order,
so `[f2]` in the prompt is always exactly `[data-pb-ref="f2"]` in the page.
Re-tagging (rather than only adding) is what keeps refs unique when a SPA
re-renders and old nodes survive: a stale `f5` on a detached node would
otherwise collide with a fresh `f5` on a live one.

Element that exists but has no box (display:none, zero size) is marked
`data-pb-offscreen`, and pageview drops it: an agent that "fills" an invisible
field has learned nothing and changed nothing.
"""

import os
import time

# The project owns its browser binaries. `ai_Pobisk.sh` exports this, and the
# setdefault here covers direct imports (tests, `python -m`): a dependency that
# writes 300 MB into a global cache is a dependency that has to be cleaned up
# on someone else's machine.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BROWSERS = os.path.join(_REPO, ".playwright")
if os.path.isdir(_BROWSERS):
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", _BROWSERS)


class BrowserError(RuntimeError):
    """The page could not be driven: missing browser, timeout, dead tab."""


# Clear stale refs, then number every interactive element in document order.
# The selector must stay in sync with pageview.INTERACTIVE; both are documented
# there. Returning outerHTML (not a JSON tree) keeps pageview the only parser.
TAG_JS = r"""
() => {
  const sel = 'input:not([type=hidden]),select,textarea,button,a[href]';
  document.querySelectorAll('[data-pb-ref]').forEach(el => el.removeAttribute('data-pb-ref'));
  document.querySelectorAll('[data-pb-offscreen]').forEach(el => el.removeAttribute('data-pb-offscreen'));

  // Tabbed modals stack both panels in the same grid cell and fade the
  // inactive one out with opacity:0. The element itself is still "visible",
  // sized and on screen, so the only honest test is the ancestor chain.
  // Without this, a login form and a signup form look identical to the agent
  // and it fills the wrong one — which is exactly what happened on xkiro.com.
  const hiddenByAncestor = (el) => {
    let n = el;
    for (let i = 0; i < 8 && n && n !== document.documentElement; i++, n = n.parentElement) {
      const s = getComputedStyle(n);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return true;
      if (n.getAttribute('aria-hidden') === 'true' || n.hasAttribute('hidden')) return true;
    }
    return false;
  };

  const els = Array.from(document.querySelectorAll(sel));
  els.forEach((el, i) => {
    el.setAttribute('data-pb-ref', 'f' + (i + 1));
    const tag = el.tagName;
    // outerHTML serialises *attributes*, not live properties. A field filled
    // by locator.fill() keeps value="" in the markup, so the next snapshot
    // would report it empty and the loop would fill it again forever.
    if (tag === 'INPUT') {
      const type = (el.type || '').toLowerCase();
      if (type === 'checkbox' || type === 'radio') {
        if (el.checked) { el.setAttribute('checked', ''); }
        else { el.removeAttribute('checked'); }
      } else if (el.value !== (el.getAttribute('value') || '')) {
        el.setAttribute('value', el.value);
      }
    } else if (tag === 'TEXTAREA') {
      el.textContent = el.value;
    } else if (tag === 'SELECT') {
      el.setAttribute('data-pb-value', el.value);
    }
    const box = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    const invisible = (box.width === 0 && box.height === 0)
                   || style.visibility === 'hidden' || style.display === 'none'
                   || hiddenByAncestor(el)
                   || box.right <= 0 || box.left >= window.innerWidth;
    if (invisible) el.setAttribute('data-pb-offscreen', '1');
  });

  // A captcha script in the page is not a captcha the human must pass: v3 /
  // invisible widgets mint a token on their own. Only a widget that is on
  // screen and has a size asks for a click, and only that one is a blocker.
  document.querySelectorAll('iframe[data-pb-captcha]').forEach(f => f.removeAttribute('data-pb-captcha'));
  document.querySelectorAll('iframe').forEach(f => {
    const src = (f.getAttribute('src') || '').toLowerCase();
    if (!/hcaptcha|recaptcha|turnstile|challenges\.cloudflare/.test(src)) return;
    const b = f.getBoundingClientRect();
    if (b.width > 20 && b.height > 20 && !hiddenByAncestor(f)) {
      f.setAttribute('data-pb-captcha', 'visible');
    }
  });
  return document.documentElement.outerHTML;
}
"""


def available():
    """Is Playwright importable? Cheap, and safe to call in any command."""
    try:
        import playwright.sync_api  # noqa: F401
        return True
    except Exception:
        return False


def install_hint():
    """What to run when Playwright is missing. The project, not the machine."""
    return ("browser automation needs Playwright, installed into this repo:\n"
            "  python3 -m venv .venv\n"
            "  .venv/bin/pip install -r requirements.txt\n"
            "  PLAYWRIGHT_BROWSERS_PATH=$PWD/.playwright "
            ".venv/bin/python -m playwright install chromium\n"
            "then ./ai_Pobisk.sh — it picks up .venv/bin/python by itself.")


class PlaywrightDriver:
    """One persistent Chromium profile per site: cookies survive between runs."""

    def __init__(self, profile_dir, headless=True, timeout_ms=20000,
                 slow_mo_ms=0, viewport=(1280, 900), channel=None):
        self.profile_dir = profile_dir
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.slow_mo_ms = slow_mo_ms
        self.viewport = {"width": viewport[0], "height": viewport[1]}
        # "chrome" runs the installed Google Chrome instead of bundled
        # Chromium. It matters for exactly one thing: Google's sign-in flow
        # trusts Chrome and rejects the bundled build with "this browser may
        # not be secure". For every other site the bundled one is fine.
        self.channel = channel
        self._pw = None
        self._ctx = None
        self.page = None

    # ── lifecycle ─────────────────────────────────────────────────────────────
    def start(self):
        if self.page is not None:
            return self
        if not available():
            raise BrowserError(install_hint())
        from playwright.sync_api import sync_playwright
        os.makedirs(self.profile_dir, exist_ok=True)
        try:
            self._pw = sync_playwright().start()
            opts = dict(headless=self.headless, slow_mo=self.slow_mo_ms,
                        viewport=self.viewport, locale="ru-RU",
                        args=["--disable-blink-features=AutomationControlled"])
            if self.channel:
                opts["channel"] = self.channel
            self._ctx = self._pw.chromium.launch_persistent_context(
                self.profile_dir, **opts)
            self._ctx.set_default_timeout(self.timeout_ms)
            self.page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            self.page.on("dialog", lambda d: d.accept())
            return self
        except Exception as e:
            self.close()
            raise BrowserError("cannot start Chromium: %s" % e)

    def close(self):
        try:
            if self._ctx is not None:
                self._ctx.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._pw = self.page = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # ── navigation ────────────────────────────────────────────────────────────
    def goto(self, url, wait_until="domcontentloaded"):
        self.start()
        try:
            self.page.goto(url, wait_until=wait_until, timeout=self.timeout_ms)
        except Exception as e:
            raise BrowserError("goto %s failed: %s" % (url, str(e).splitlines()[0]))
        self._settle()
        return self

    def url(self):
        try:
            return self.page.url if self.page else ""
        except Exception:
            return ""

    def title(self):
        try:
            return self.page.title() if self.page else ""
        except Exception:
            return ""

    def _settle(self, quiet_ms=400):
        """Wait for the network to go quiet, but never block the loop on it."""
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass
        try:
            self.page.wait_for_timeout(quiet_ms)
        except Exception:
            time.sleep(quiet_ms / 1000.0)
        self._follow_popup()

    def _follow_popup(self):
        """OAuth and 'open in new tab' flows move the target page."""
        try:
            pages = [p for p in self._ctx.pages if not p.is_closed()]
            if pages and pages[-1] is not self.page:
                self.page = pages[-1]
        except Exception:
            pass

    # ── reading ───────────────────────────────────────────────────────────────
    def html(self):
        self.start()
        try:
            return self.page.evaluate(TAG_JS) or ""
        except Exception as e:
            raise BrowserError("snapshot failed: %s" % str(e).splitlines()[0])

    def screenshot(self, path):
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            self.page.screenshot(path=path, full_page=False)
            return path
        except Exception:
            return ""

    # ── acting ────────────────────────────────────────────────────────────────
    def _loc(self, ref):
        if not ref:
            raise BrowserError("action needs a target ref")
        loc = self.page.locator('[data-pb-ref="%s"]' % ref)
        try:
            if loc.count() == 0:
                raise BrowserError("no element with ref %s on the page" % ref)
        except BrowserError:
            raise
        except Exception:
            pass
        return loc.first

    def fill(self, ref, value):
        loc = self._loc(ref)
        try:
            loc.fill(str(value))
        except Exception:
            # contenteditable and some custom widgets only accept typing.
            loc.click()
            self.page.keyboard.type(str(value))
        return True

    def click(self, ref):
        self._loc(ref).click()
        self._settle()
        return True

    def check(self, ref, on=True):
        loc = self._loc(ref)
        loc.check() if on else loc.uncheck()
        return True

    def select(self, ref, value):
        loc = self._loc(ref)
        try:
            loc.select_option(label=str(value))
        except Exception:
            loc.select_option(value=str(value))
        return True

    def press(self, ref, key="Enter"):
        if ref:
            self._loc(ref).press(key)
        else:
            self.page.keyboard.press(key)
        self._settle()
        return True

    def wait(self, ms=1000):
        try:
            self.page.wait_for_timeout(int(ms))
        except Exception:
            time.sleep(int(ms) / 1000.0)
        return True
