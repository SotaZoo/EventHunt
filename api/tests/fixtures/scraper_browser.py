"""Minimal fake of the external Playwright page API used by detail scrapers."""
import json
from contextlib import nullcontext
from types import SimpleNamespace


class ScraperPage:
    def __init__(self, links, *, description="Oct 7, 2026 | Betaworks • New York", body="5:30pm",
                 html="", app_meta=None):
        self.links = links
        self.description = description
        self.body = body
        self.html = html
        self.app_meta = app_meta or []
        self.visited = []
        self.mouse = SimpleNamespace(wheel=lambda *args: None)

    def goto(self, url, **kwargs):
        self.visited.append(url)

    def wait_for_timeout(self, delay):
        pass

    def eval_on_selector_all(self, selector, expression):
        if selector.startswith("meta"):
            return self.app_meta
        if selector.startswith("script"):
            return [json.dumps({"@type": "Event", "name": "Open event", "startDate": "2026-10-07T18:00:00-04:00"})]
        return self.links

    def eval_on_selector(self, selector, expression):
        if "og:title" in selector:
            return "Betaworks event"
        if "og:description" in selector:
            return self.description
        return None

    def evaluate(self, expression):
        return self.body

    def content(self):
        return self.html


def playwright_factory(page):
    context = SimpleNamespace(new_page=lambda: page)
    browser = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: None)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
    return lambda: nullcontext(playwright)
