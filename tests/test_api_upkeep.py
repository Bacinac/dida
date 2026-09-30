"""Address-book upkeep — the decisions, not the network.

This is the one module that writes into somebody else's system, so what is
pinned here is what it would DO: which entry counts as empty, which pair counts
as the same person, where an unlabelled contact is filed, and how a written-out
name goes back into given and family. Every one of those, got wrong, changes a
real person's card in a real address book.
"""
from dida_api.upkeep import (
    _duplicate_findings,
    _empty_proposals,
    _has_diacritics,
    _label_proposals,
)

LABELS = {"Work": "contactGroups/work", "Private": "contactGroups/private"}


def person(name, *, rid=None, mail=None, phone=None, born=False, org=None, labels=()):
    given, _, family = name.rpartition(" ")
    p = {
        "resourceName": rid or f"people/{name.replace(' ', '')}",
        "names": [{"displayName": name, "givenName": given or name, "familyName": family}],
        "metadata": {"sources": [{"etag": "%eTag"}]},
    }
    if mail:
        p["emailAddresses"] = [{"value": mail}]
    if phone:
        p["phoneNumbers"] = [{"value": phone}]
    if born:
        p["birthdays"] = [{"date": {"year": 1980, "month": 1, "day": 1}}]
    if org:
        p["organizations"] = [{"name": org}]
    if labels:
        p["memberships"] = [
            {"contactGroupMembership": {"contactGroupId": g.rsplit("/", 1)[-1],
                                        "contactGroupResourceName": g}} for g in labels
        ]
    return p


def test_a_birthday_is_a_whole_date_or_it_is_not_one():
    """The year is the entire reason the house keeps these — a birthday without
    one cannot say how old anybody is turning. Google holds those as --MM-DD, and
    they come back as something to SEE and complete, never as a date."""
    from dida_api.upkeep import _born_of, _parse_born

    assert _born_of(person("Ana", born=True)) == ("1980-01-01", "")
    assert _born_of({"birthdays": [{"date": {"month": 6, "day": 15}}]}) == ("", "--06-15")
    assert _born_of({}) == ("", "")

    assert _parse_born("1975-07-19") == {"year": 1975, "month": 7, "day": 19}
    # A date the calendar does not have is refused here, not by Google.
    assert _parse_born("1974-02-31") is None
    assert _parse_born("--07-19") is None
    assert _parse_born("") is None


def test_only_a_card_with_nothing_on_it_is_empty():
    book = [
        person("Prazan Kontakt"),
        person("Ima Broj", phone="+385 91 111 2222"),
        person("Ima Mail", mail="tko@example.com"),
        person("Ima Rodjendan", born=True),
        person("Ima Tvrtku", org="Tvrtka d.o.o."),
    ]
    found = _empty_proposals(book)
    assert [p.name for p in found] == ["Prazan Kontakt"]
    # The row has to say what is missing: this one asks for a deletion, and a
    # deletion nobody can see the grounds for is one nobody should tick.
    assert all(word in found[0].detail.lower()
               for word in ("broja", "maila", "rođendana", "tvrtke"))
    assert found[0].after == "—"


def test_the_same_person_is_reported_once_per_pair_and_never_applied():
    book = [
        person("Ivan Ivic", rid="people/a", mail="ivan@example.com"),
        person("Ivan Ivic", rid="people/b", phone="+385 91 555 6677"),
        person("Netko Drugi", rid="people/c", phone="0915556677"),
    ]
    found = _duplicate_findings(book)
    # a+b share a name; b+c share a number (last eight digits, however written).
    assert len(found) == 2
    assert all(not p.applicable for p in found), "a merge is Google's, not ours"
    assert {tuple(sorted(p.id.removeprefix("duplicate:").split("+"))) for p in found} == {
        ("people/a", "people/b"), ("people/b", "people/c"),
    }


def test_one_name_alone_is_not_a_duplicate():
    assert _duplicate_findings([person("Sam Samuel", mail="sam@example.com")]) == []


def test_filing_follows_the_card_not_a_hunch():
    book = [
        person("Radi Negdje", org="Tvrtka d.o.o.", mail="x@gmail.com"),
        person("Poslovna Adresa", mail="ime@tvrtka.example"),
        person("Osobna Adresa", mail="ime@gmail.com"),
        person("Nista O Njemu", phone="+385 91 000 1111"),
        person("Vec Oznacen", labels=("contactGroups/work",)),
    ]
    by_name = {p.name: p for p in _label_proposals(book, LABELS)}
    assert "Vec Oznacen" not in by_name, "somebody already filed is not re-filed"
    assert by_name["Radi Negdje"].after == "Work"
    assert by_name["Poslovna Adresa"].after == "Work"
    assert by_name["Osobna Adresa"].after == "Private"
    assert by_name["Nista O Njemu"].after == "Private"
    # A guess with nothing behind it says so, and is not pre-ticked by the page.
    assert by_name["Radi Negdje"].sure is True
    assert by_name["Nista O Njemu"].sure is False


def test_nothing_is_filed_when_the_labels_do_not_exist():
    # An installation whose book has no Work/Private is not one to invent them in.
    assert _label_proposals([person("Bilo Tko")], {"Obitelj": "contactGroups/x"}) == []


def test_a_name_that_already_carries_its_marks_is_left_alone():
    assert _has_diacritics("Bošković") and _has_diacritics("Đurđević")
    assert _has_diacritics("Krznarić") and _has_diacritics("Žarko")
    assert not _has_diacritics("Boskovic")
    assert not _has_diacritics("Ana Marija Kovacic")


def test_the_etag_that_is_written_is_the_one_google_checks():
    """A contact carries two etags that look alike and are not interchangeable.

    Sending `metadata.sources[].etag` fails every write with 400 "Request
    person.etag is different than the current person.etag" — which reads like a
    stale cache and is not one. Measured against the live API: 148 identical
    failures, and the same write succeeded the moment the person's own etag went
    in. The reference text says the other thing, so this is pinned here."""
    from dida_api.upkeep import _etag

    p = person("Tome Babic")
    p["etag"] = "%EgsBAgcJCwwuNz0+PxoEAQIFByIM"
    p["metadata"] = {"sources": [{"etag": "#euwPitXZO7A="}]}
    assert _etag(p) == "%EgsBAgcJCwwuNz0+PxoEAQIFByIM"
    # Only when a person carries no etag of its own does the source one stand in.
    del p["etag"]
    assert _etag(p) == "#euwPitXZO7A="


def test_a_failure_says_what_google_said():
    """A bare status line put 148 identical rows on the screen with nothing in
    them to act on. The body named the field."""
    import httpx
    from dida_api.upkeep import _why

    resp = httpx.Response(400, json={"error": {
        "code": 400, "status": "FAILED_PRECONDITION",
        "message": "Request person.etag is different than the current person.etag."}})
    assert "person.etag" in _why(resp)
    # A body that is not the shape we expect still has to yield something usable.
    assert "418" in _why(httpx.Response(418, text="teapot"))


def test_a_row_says_where_the_person_is_already_filed():
    """The chips beside a name show Google's answer, not a blank slate.

    Without this every one of 149 rows offered Work and Private with neither lit,
    on contacts that were all already filed — which reads as "none of these apply"
    and asks somebody to click through the whole book to restate what is known."""
    from dida_api.upkeep import _label_names

    p = person("Vec Oznacen", labels=("contactGroups/work", "contactGroups/ice"))
    names = _label_names(p, {**LABELS, "ICE": "contactGroups/ice"})
    assert set(names) == {"Work", "ICE"}
    assert _label_names(person("Bez Oznake"), LABELS) == []


def test_filing_is_the_two_this_page_offers_and_no_others():
    """Moving somebody from Work to Private must take them OUT of Work, or they
    end up in both. Whatever else they carry — ICE — is not this page's business."""
    from dida_api.upkeep import _FILING

    assert _FILING == ("Work", "Private")
    assert "ICE" not in _FILING


def test_a_correction_does_not_take_the_middle_name_with_it():
    """`updateMask=names` replaces the WHOLE name. Sending only given and family
    deletes whatever else was in there — and one contact in this book carries a
    middle name, which would vanish during a spelling correction with nothing to
    show for it."""
    from dida_api.upkeep import _extra_name, _rename

    p = person("Ana Petrovic Novak")
    p["names"][0].update({"givenName": "Ana", "middleName": "Petrovic",
                          "familyName": "Novak", "displayNameLastFirst": "Novak, Ana"})
    written = _rename(p, "Ana", "Novak")
    assert written["middleName"] == "Petrovic", "the middle name survives the write"
    assert written["givenName"] == "Ana" and written["familyName"] == "Novak"
    # What Google computes is not sent back: a stale display name would be pinned
    # over the corrected one.
    assert "displayName" not in written and "displayNameLastFirst" not in written
    # And the row says it is there, because hiding it is how it got lost.
    assert "Petrovic" in _extra_name(p)
    assert _extra_name(person("Bez Srednjeg")) == ""


def test_a_write_never_leaves_a_masked_field_out():
    """`batchUpdateContacts` takes ONE updateMask for the whole call, and a body
    that omits a masked field does not leave it alone — Google CLEARS it.

    This is not hypothetical. Eleven people had their names deleted while their
    birthdays were being added: their bodies carried only `birthdays`, another row
    in the same batch carried `names`, and the shared mask said `names,birthdays`.
    Every body is therefore complete by construction, carrying the current value
    of whatever did not change."""
    from dida_api.upkeep import _WRITE_MASK, _write_body

    p = person("Katarina Radosevic", born=True)
    # Only the birthday moves; the name must still be in the body, unchanged.
    body = _write_body(p, "Katarina", "Radosevic", [{"date": {"year": 1981, "month": 6, "day": 11}}])
    assert body["names"] == [{"givenName": "Katarina", "familyName": "Radosevic"}]
    assert body["birthdays"] == [{"date": {"year": 1981, "month": 6, "day": 11}}]

    # Only the name moves; the birthday must still be in the body, unchanged.
    body = _write_body(p, "Katarina", "Radošević", None)
    assert body["names"] == [{"givenName": "Katarina", "familyName": "Radošević"}]
    assert body["birthdays"] == [{"date": {"year": 1980, "month": 1, "day": 1}}]

    # Both fields are in the mask, so both must be in every body.
    assert set(_WRITE_MASK.split(",")) <= set(body), (
        "a masked field missing from the body is a deletion, not a no-op")


def test_clearing_a_birthday_is_told_apart_from_not_touching_one():
    """An empty list clears; None means "leave whatever is there". Conflating the
    two either deletes every birthday or makes them impossible to remove."""
    from dida_api.upkeep import _write_body

    p = person("Ima Rodjendan", born=True)
    assert _write_body(p, "Ima", "Rodjendan", [])["birthdays"] == []
    assert _write_body(p, "Ima", "Rodjendan", None)["birthdays"] == [
        {"date": {"year": 1980, "month": 1, "day": 1}}]
