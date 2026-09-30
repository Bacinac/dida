"""The People API half: a refresh token in, a contacts book out.

Google leaves no single good way in, so there are two. This is the one without a
file: the same book, read over the API — and made awkward on purpose. The flow
for machines with no browser does not allow a contacts scope at all, and an app
left in "Testing" is issued a refresh token that dies after seven days. So the
consent runs in a browser (dida_api.contacts) against a PUBLISHED app, and what
lands here is a refresh token that lasts.

The other way in is a file — contacts.google.com exports vCard or CSV, nothing
expires and nothing has to be authorised. It is read by `dida_core.people` and
uploaded through the API; it stays the way in when this one is refused.
"""

from __future__ import annotations

import httpx
from dida_core.people import parse_birthday

EXCHANGE = "https://oauth2.googleapis.com/token"
CONNECTIONS = "https://people.googleapis.com/v1/people/me/connections"


async def access_token(client_id: str, client_secret: str, refresh_token: str) -> str:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(EXCHANGE, data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
    if resp.status_code != 200:
        # Loud, and with Google's own words: the two failures worth telling apart
        # are a revoked grant and a seven-day death in "Testing", and only the body
        # says which.
        raise RuntimeError(f"Google refused the refresh token: {resp.text[:200]}")
    return resp.json()["access_token"]


async def read_book(client_id: str, client_secret: str, refresh_token: str) -> list[dict]:
    """Everybody in the book who has a name and a birthday.

    Complete or not at all: a half-read page set is returned to nobody, because
    the caller prunes against it and a short read would look like people leaving.
    """
    token = await access_token(client_id, client_secret, refresh_token)
    out: list[dict] = []
    page = None
    async with httpx.AsyncClient(timeout=60) as client:
        while True:
            resp = await client.get(CONNECTIONS, params={
                "personFields": "names,birthdays", "pageSize": 1000,
                **({"pageToken": page} if page else {}),
            }, headers={"Authorization": f"Bearer {token}"})
            resp.raise_for_status()
            body = resp.json()
            for person in body.get("connections", []):
                names = person.get("names") or []
                days = person.get("birthdays") or []
                if not names or not days:
                    continue
                date = days[0].get("date") or {}
                out.append({
                    "name": names[0].get("displayName", ""),
                    "born": parse_birthday(date.get("year"), date.get("month"), date.get("day")),
                    "raw": days[0].get("text") or str(date),
                    "id": person.get("resourceName", ""),
                })
            page = body.get("nextPageToken")
            if not page:
                break
    return out
