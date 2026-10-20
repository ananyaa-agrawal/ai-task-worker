"""Browser tools backed by Playwright (Chromium).

The model sees each page as compact text with element refs ([e12]) instead of raw HTML or
pixels: cheap, precise, and generic across sites. Screenshots are available on demand
(browser_screenshot) and are captured automatically after every action as evidence.
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from playwright.sync_api import Browser, Error as PlaywrightError, Playwright, sync_playwright

from ..policy import Assessment, classify_activation
from .base import Tool, ToolError, ToolResult, obj

if TYPE_CHECKING:
    from ..runtime import RunContext

SNAPSHOT_JS = (Path(__file__).parent / "snapshot.js").read_text()
MAX_SNAPSHOT_CHARS = 14000

ELEMENT_INFO_JS = """(el) => {
  const form = el.form || el.closest('form');
  const submits = !!form && (
    (el.tagName === 'BUTTON' && (el.getAttribute('type') || 'submit') === 'submit') ||
    (el.tagName === 'INPUT' && ['submit', 'image'].includes((el.type || '').toLowerCase())));
  const fields = {};
  if (form) for (const f of form.elements) if (f.name && f.type !== 'hidden') fields[f.name] = f.type === 'password' ? '***' : (f.value || '').slice(0, 80);
  return {
    tag: el.tagName.toLowerCase(), type: el.type || '', href: el.href || '',
    text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 120),
    submits,
    form: form ? { method: form.getAttribute('method') || 'get', action: form.action || location.href,
                   has_password: !!form.querySelector('input[type=password]'), fields } : null,
  };
}"""


class BrowserHost:
    """One Playwright + Chromium process, shared by the worker and the verifier sessions."""

    def __init__(self, headless: bool = True) -> None:
        self._pw: Playwright = sync_playwright().start()
        self.browser: Browser = self._pw.chromium.launch(headless=headless)

    def new_session(self, ctx: "RunContext", label: str) -> "BrowserSession":
        return BrowserSession(self, ctx, label)

    def close(self) -> None:
        try:
            self.browser.close()
        finally:
            self._pw.stop()


class BrowserSession:
    def __init__(self, host: BrowserHost, ctx: "RunContext", label: str) -> None:
        self.ctx = ctx
        self.label = label
        self.context = host.browser.new_context(accept_downloads=True, viewport={"width": 1280, "height": 860})
        self.context.route("**/*", self._guard)
        self.page = self.context.new_page()
        self.page.set_default_timeout(10000)
        self._downloads: list = []
        self._last_status: int | None = None
        self.page.on("download", lambda d: self._downloads.append(d))
        self.page.on("response", self._on_response)
        self.blocked: list[str] = []

    # ------------------------------------------------------------------ plumbing
    def _guard(self, route) -> None:
        url = route.request.url
        if not self.ctx.url_allowed(url):
            self.blocked.append(url)
            route.abort("blockedbyclient")
        else:
            route.continue_()

    def _on_response(self, response) -> None:
        try:
            if response.request.resource_type == "document" and response.frame == self.page.main_frame:
                self._last_status = response.status
        except PlaywrightError:
            pass

    def _settle(self) -> None:
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=8000)
            self.page.wait_for_load_state("networkidle", timeout=2500)
        except PlaywrightError:
            pass

    def _save_downloads(self) -> list[str]:
        saved = []
        out_dir = self.ctx.workspace / "downloads"
        out_dir.mkdir(parents=True, exist_ok=True)
        while self._downloads:
            d = self._downloads.pop(0)
            name = re.sub(r"[^A-Za-z0-9._-]", "_", d.suggested_filename or "download.bin")
            target = out_dir / name
            d.save_as(target)
            saved.append(str(target.relative_to(self.ctx.workspace)))
        return saved

    def locate(self, ref: str):
        if not re.fullmatch(r"e\d+", ref or ""):
            raise ToolError(f"'{ref}' is not a valid element ref; use refs like 'e12' from the latest page snapshot.")
        loc = self.page.locator(f'[data-tw-ref="{ref}"]')
        if loc.count() == 0:
            raise ToolError(f"Element {ref} is not on the current page (the page probably changed). "
                            "Use refs from the most recent snapshot.")
        return loc.first

    def element_info(self, ref: str) -> dict:
        return self.locate(ref).evaluate(ELEMENT_INFO_JS)

    # ------------------------------------------------------------------ observation
    def observe(self, note: str = "", downloads: list[str] | None = None) -> ToolResult:
        snap = None
        for attempt in range(3):  # the page may still be navigating (e.g. after a redirect)
            self._settle()
            try:
                snap = self.page.evaluate(SNAPSHOT_JS, MAX_SNAPSHOT_CHARS)
                break
            except PlaywrightError as e:
                if attempt == 2:
                    raise ToolError(f"Could not read the page: {str(e).splitlines()[0]}")
                time.sleep(0.5)
        header = [f"URL: {snap['url']}", f"Title: {snap['title']}"]
        status = self._last_status
        if status and status >= 400:
            header.append(f"HTTP status: {status} (the page returned an error)")
        if snap["notices"]:
            header.append("Page notices: " + " | ".join(snap["notices"]))
        if downloads:
            header.append("Downloaded: " + ", ".join(downloads) + "  (open with read_file)")
        if self.blocked:
            header.append("Blocked by policy (outside allowed systems): " + ", ".join(self.blocked[-3:]))
            self.blocked.clear()
        if note:
            header.insert(0, note)
        body = snap["text"] + ("\n[... page truncated ...]" if snap["truncated"] else "")
        shot = self.capture("auto")
        return ToolResult("\n".join(header) + "\n---\n" + body,
                          data={"url": snap["url"], "status": status, "screenshot": shot, "downloads": downloads or []})

    def capture(self, label: str) -> str | None:
        path = self.ctx.trace.shot_path(f"{self.label}-{label}")
        try:
            self.page.screenshot(path=str(path), type="jpeg", quality=60)
        except PlaywrightError:
            return None
        rel = str(path.relative_to(self.ctx.trace.run_dir))
        self.ctx.trace.emit("screenshot", path=rel, session=self.label, url=self.page.url)
        return rel

    # ------------------------------------------------------------------ actions
    def open(self, url: str) -> ToolResult:
        url = urljoin(self.ctx.config.sandbox_url + "/", url) if not urlparse(url).scheme else url
        if not self.ctx.url_allowed(url):
            raise ToolError(f"{url} is outside the systems this worker may access. Allowed hosts: "
                            f"{', '.join(sorted(self.ctx.config.allowed_hosts))}")
        self._last_status = None
        try:
            resp = self.page.goto(url, wait_until="domcontentloaded")
            if resp:
                self._last_status = resp.status
        except PlaywrightError as e:
            if "Download is starting" not in str(e) and "ERR_ABORTED" not in str(e):
                raise ToolError(f"Navigation failed: {str(e).splitlines()[0]}")
            time.sleep(0.5)
        return self.observe(downloads=self._save_downloads())

    def click(self, ref: str) -> ToolResult:
        el = self.locate(ref)
        self._last_status = None
        url_before = self.page.url
        try:
            el.click(timeout=6000)
        except PlaywrightError as e:
            raise ToolError(f"Could not click {ref}: {str(e).splitlines()[0]}")
        # A click either navigates, changes the page in place, or starts a download (which fires
        # asynchronously). Give a download a moment to start when the page didn't navigate.
        deadline = time.time() + 1.5
        while time.time() < deadline and not self._downloads and self.page.url == url_before:
            self.page.wait_for_timeout(100)
        return self.observe(downloads=self._save_downloads())

    def type(self, ref: str, text: str, submit: bool) -> ToolResult:
        el = self.locate(ref)
        if submit and el.evaluate("e => e.tagName") == "TEXTAREA":
            raise ToolError("Enter only adds a new line in a multi-line text area; nothing was typed. Type with "
                            "submit=false, then click the form's submit button.")
        try:
            el.fill(text, timeout=5000)
            if submit:
                self._last_status = None
                el.press("Enter")
                time.sleep(0.3)
        except PlaywrightError as e:
            raise ToolError(f"Could not type into {ref}: {str(e).splitlines()[0]}")
        if submit:
            return self.observe(downloads=self._save_downloads())
        return ToolResult(f"Typed into {ref}. The field now holds the text; nothing has been submitted yet.")

    def select(self, ref: str, option: str) -> ToolResult:
        el = self.locate(ref)
        try:
            el.select_option(label=option, timeout=4000)
        except PlaywrightError:
            try:
                el.select_option(value=option, timeout=2000)
            except PlaywrightError as e:
                raise ToolError(f"Option '{option}' not found in {ref}: {str(e).splitlines()[0]}")
        chosen = el.evaluate("s => s.options[s.selectedIndex]?.text || ''")
        return ToolResult(f"Selected '{chosen}' in {ref}.")

    def fill_secret(self, ref: str, secret_name: str) -> ToolResult:
        value = self.ctx.secrets.get(secret_name)
        if value is None:
            raise ToolError(f"Unknown secret '{secret_name}'. Available: {', '.join(sorted(self.ctx.secrets)) or 'none'}")
        try:
            self.locate(ref).fill(value, timeout=5000)
        except PlaywrightError as e:
            raise ToolError(f"Could not fill {ref}: {str(e).splitlines()[0]}")
        return ToolResult(f"Filled {ref} with secret '{secret_name}' (value hidden).")

    def screenshot(self) -> ToolResult:
        rel = self.capture("requested")
        if not rel:
            raise ToolError("Screenshot failed.")
        data = base64.standard_b64encode((self.ctx.trace.run_dir / rel).read_bytes()).decode()
        return ToolResult([{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
                           {"type": "text", "text": f"Screenshot of {self.page.url} saved as evidence: {rel}"}],
                          data={"screenshot": rel})

    def back(self) -> ToolResult:
        self._last_status = None
        self.page.go_back(wait_until="domcontentloaded")
        return self.observe()


# ---------------------------------------------------------------------- tool definitions

def _assess_click(args: dict, ctx: "RunContext") -> Assessment:
    return classify_activation(ctx.browser().element_info(args["ref"]))


def _assess_type(args: dict, ctx: "RunContext") -> Assessment:
    if not args.get("submit"):
        return Assessment("read", f"type into {args['ref']}")
    info = ctx.browser().element_info(args["ref"])
    if info["tag"] == "textarea":  # Enter adds a newline; the tool refuses this combination
        return Assessment("read", f"type into {args['ref']}")
    info["submits"] = True  # pressing Enter in a form field submits the form
    return classify_activation(info)


def _assess_open(args: dict, ctx: "RunContext") -> Assessment:
    url = args["url"]
    full = urljoin(ctx.config.sandbox_url + "/", url) if not urlparse(url).scheme else url
    if not ctx.url_allowed(full):
        return Assessment("blocked", f"open {full}", "outside the allowed systems")
    return Assessment("read", f"open {full}")


def browser_tools() -> list[Tool]:
    ref = {"type": "string", "description": "Element ref from the latest snapshot, e.g. 'e12'."}
    return [
        Tool("browser_open",
             "Navigate the browser to a URL (absolute, or a path relative to the company intranet) and return "
             "a text snapshot of the page. Interactive elements are shown as [eN] refs.",
             obj({"url": {"type": "string"}}),
             lambda a, c: c.browser().open(a["url"]), assess=_assess_open),
        Tool("browser_snapshot", "Return a fresh text snapshot of the current page (refs are renumbered).",
             obj({}), lambda a, c: c.browser().observe()),
        Tool("browser_click",
             "Click an element (link, button). Returns the resulting page snapshot. Clicking a link to a file "
             "downloads it into the workspace.",
             obj({"ref": ref}), lambda a, c: c.browser().click(a["ref"]), assess=_assess_click),
        Tool("browser_type",
             "Replace the contents of a text field. Set submit=true to press Enter afterwards (submits the form).",
             obj({"ref": ref, "text": {"type": "string"}, "submit": {"type": "boolean"}}, ["ref", "text"]),
             lambda a, c: c.browser().type(a["ref"], a["text"], bool(a.get("submit"))), assess=_assess_type),
        Tool("browser_select", "Choose an option in a <select> dropdown by its visible text.",
             obj({"ref": ref, "option": {"type": "string"}}),
             lambda a, c: c.browser().select(a["ref"], a["option"])),
        Tool("browser_fill_secret",
             "Fill a field with a stored credential without revealing it, e.g. secret_name='ledgerlite.password'. "
             "Never type passwords yourself.",
             obj({"ref": ref, "secret_name": {"type": "string"}}),
             lambda a, c: c.browser().fill_secret(a["ref"], a["secret_name"])),
        Tool("browser_back", "Go back to the previous page.", obj({}), lambda a, c: c.browser().back()),
        Tool("browser_screenshot",
             "Take a screenshot of the current page. Use when the text snapshot is not enough, or to capture "
             "evidence of a completed action.",
             obj({}), lambda a, c: c.browser().screenshot()),
    ]
