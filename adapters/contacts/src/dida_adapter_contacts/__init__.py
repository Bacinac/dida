"""DIDA contacts adapter — the household's address book.

A *source* adapter with no hardware: it reads the contacts book (Google People
API, or an export file when OAuth is refused) into the `people` table once a day,
and publishes what the house needs to know today — whether anyone has a birthday
in the announce window, and who.

Why DIDA and not the photo library: a birth date is live household state, and the
reader has to be ONE. Two readers mean two consent screens, two tokens to expire
and two schedules that drift apart. OPUS Library asks over HTTP.

The OAuth handshake itself lives in the API service (`dida_api.contacts`) —
consent happens in a browser, against a public callback. This process only holds
the resulting refresh token and rolls it into access tokens, exactly as the
smartthings adapter does.
"""

from __future__ import annotations

from dida_adapter_contacts.adapter import ContactsAdapter

__all__ = ["ContactsAdapter"]
