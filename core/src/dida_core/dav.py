"""Reading an address book and a calendar over WebDAV (CardDAV RFC 6352, CalDAV RFC 4791).

Sign in, find the user's home from the well-known address (RFC 6764), list its
collections and read what they hold. Read only: the book and the calendar are kept
where they live, DIDA keeps what it read from them.

Needs httpx, which only the adapters that read DAV carry; nothing else in core
imports this module.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx

__all__ = ["Collection", "Dav", "DavError"]

D = "DAV:"
CARD = "urn:ietf:params:xml:ns:carddav"
CAL = "urn:ietf:params:xml:ns:caldav"
_KIND = {
    "carddav": (f"{{{CARD}}}addressbook-home-set", f"{{{CARD}}}addressbook"),
    "caldav": (f"{{{CAL}}}calendar-home-set", f"{{{CAL}}}calendar"),
}


class DavError(RuntimeError):
    pass


class Collection:
    def __init__(self, href: str, name: str) -> None:
        self.href, self.name = href, name

    def __repr__(self) -> str:
        return f"Collection({self.href!r}, {self.name!r})"


def _propfind(*names: str) -> bytes:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><d:propfind xmlns:d="{D}" '
        f'xmlns:card="{CARD}" xmlns:cal="{CAL}"><d:prop>{"".join(names)}</d:prop></d:propfind>'
    ).encode()


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


class Dav:
    """One signed-in conversation with a DAV server: `async with Dav(url, email, password)`."""

    def __init__(self, url: str, email: str, password: str, *, timeout: float = 30) -> None:
        if not url or not email or not password:
            raise DavError("address, e-mail and app password are all required")
        self._base = url.rstrip("/") + "/"
        self._client = httpx.AsyncClient(
            auth=(email, password), timeout=timeout, follow_redirects=True)

    async def __aenter__(self) -> Dav:
        return self

    async def __aexit__(self, *exc) -> None:
        await self._client.aclose()

    async def _ask(self, method: str, path: str, body: bytes, depth: str) -> ET.Element:
        url = urljoin(self._base, path)
        try:
            resp = await self._client.request(method, url, content=body, headers={
                "Depth": depth, "Content-Type": "application/xml; charset=utf-8"})
        except httpx.HTTPError as exc:
            raise DavError(f"{url}: {exc}") from exc
        if resp.status_code == 401:
            raise DavError("the server refused the sign-in: check the e-mail and the app password")
        if resp.status_code != 207:
            raise DavError(f"{method} {url}: HTTP {resp.status_code}")
        try:
            return ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise DavError(f"{method} {url}: not a multistatus ({exc})") from exc

    @staticmethod
    def _href(root: ET.Element, prop: str) -> str:
        found = root.find(f".//{prop}/{{{D}}}href")
        if found is None or not (found.text or "").strip():
            raise DavError(f"the server names no {prop.rpartition('}')[2]}")
        return found.text.strip()

    async def home(self, kind: str) -> str:
        """The user's address-book (`carddav`) or calendar (`caldav`) home."""
        home_set, _ = _KIND[kind]
        root = await self._ask("PROPFIND", f".well-known/{kind}",
                               _propfind("<d:current-user-principal/>"), "0")
        principal = self._href(root, f"{{{D}}}current-user-principal")
        tag = home_set.rpartition("}")[2]
        prefix = "card" if kind == "carddav" else "cal"
        root = await self._ask("PROPFIND", principal, _propfind(f"<{prefix}:{tag}/>"), "0")
        return self._href(root, home_set)

    async def collections(self, kind: str) -> list[Collection]:
        """The address books or calendars in the user's home, in the server's order."""
        _, marker = _KIND[kind]
        home = await self.home(kind)
        root = await self._ask("PROPFIND", home,
                               _propfind("<d:resourcetype/>", "<d:displayname/>"), "1")
        out = []
        for response in root.iter(f"{{{D}}}response"):
            href = (response.findtext(f"{{{D}}}href") or "").strip()
            if response.find(f".//{{{D}}}resourcetype/{marker}") is None or not href:
                continue
            out.append(Collection(href, (response.findtext(f".//{{{D}}}displayname") or "").strip()))
        return out

    @staticmethod
    def _data(root: ET.Element, prop: str) -> list[str]:
        return [each.text for each in root.iter(prop) if (each.text or "").strip()]

    async def cards(self, book: Collection) -> str:
        """Every vCard in the address book, as one stream."""
        body = (
            f'<?xml version="1.0" encoding="utf-8"?><card:addressbook-query xmlns:d="{D}" '
            f'xmlns:card="{CARD}"><d:prop><d:getetag/><card:address-data/></d:prop>'
            f'</card:addressbook-query>'
        ).encode()
        root = await self._ask("REPORT", book.href, body, "1")
        return "\r\n".join(self._data(root, f"{{{CARD}}}address-data"))

    async def events(self, calendar: Collection, start: datetime, end: datetime) -> list[str]:
        """The iCalendar objects with an event touching [start, end). A recurring event
        comes whole, master and exceptions, not expanded."""
        body = (
            f'<?xml version="1.0" encoding="utf-8"?><cal:calendar-query xmlns:d="{D}" '
            f'xmlns:cal="{CAL}"><d:prop><d:getetag/><cal:calendar-data/></d:prop>'
            f'<cal:filter><cal:comp-filter name="VCALENDAR"><cal:comp-filter name="VEVENT">'
            f'<cal:time-range start="{_stamp(start)}" end="{_stamp(end)}"/>'
            f'</cal:comp-filter></cal:comp-filter></cal:filter></cal:calendar-query>'
        ).encode()
        root = await self._ask("REPORT", calendar.href, body, "1")
        return self._data(root, f"{{{CAL}}}calendar-data")
