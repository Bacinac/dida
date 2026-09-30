"""The address-book primitives (dida_core.people): how a name is matched, when a
birth date is refused, and what the house is told today.

Three of these guard rules that are easy to "simplify" back into bugs: a name
match that insists on one spelling finds almost nobody, a birthday assembled from
a day in the book and a year from anywhere else is a date nobody has ever held,
and 29 February matched exactly is a person nobody congratulates in three years
out of four.
"""
import datetime

from dida_core.people import (
    age_turning,
    birthday_offset,
    digest,
    parse_birthday,
    plain,
    read_export,
)


def test_a_name_matches_across_spelling_case_and_order():
    assert plain("Bošković") == plain("Boskovic") == plain("BOSKOVIC")
    assert plain("Ana Marija Kovačić") == plain("Kovacic Ana Marija")
    assert plain("Đurđica") == plain("Durdica")
    # Different people still differ — the normalisation must not collapse names.
    assert plain("Ana Horvat") != plain("Ana Kovač")


def test_a_birthday_without_a_year_is_not_half_a_date():
    assert parse_birthday(1972, 5, 10) == datetime.date(1972, 5, 10)
    assert parse_birthday(None, 3, 14) is None
    assert parse_birthday("", 3, 14) is None
    assert parse_birthday(1972, 13, 40) is None


def test_an_export_reads_either_shape_google_offers():
    vcard = (
        "BEGIN:VCARD\nVERSION:3.0\nFN:Ana Horvat\nBDAY:1990-04-05\nEND:VCARD\n"
        "BEGIN:VCARD\nVERSION:3.0\nFN:Marko Marić\nBDAY:--06-15\nEND:VCARD\n"
    )
    book = read_export(vcard)
    assert [p["name"] for p in book] == ["Ana Horvat", "Marko Marić"]
    assert book[0]["born"] == datetime.date(1990, 4, 5)
    assert book[1]["born"] is None and book[1]["raw"] == "--06-15"

    csv_text = (
        "Name,First Name,Last Name,Birthday\n"
        "Ana Horvat,Ana,Horvat,1990-04-05\n"
        ",Marko,Marić,--06-15\n"
    )
    book = read_export(csv_text)
    assert book[0]["born"] == datetime.date(1990, 4, 5)
    assert book[1]["name"] == "Marko Marić" and book[1]["born"] is None


def test_a_birthday_is_found_only_inside_the_window():
    today = datetime.date(2026, 6, 15)
    born = datetime.date(1990, 6, 16)
    assert birthday_offset(born, today, within=1) == 1
    assert birthday_offset(born, today, within=0) is None
    assert birthday_offset(datetime.date(1990, 6, 15), today, within=1) == 0


def test_a_leap_day_is_congratulated_in_a_common_year():
    born = datetime.date(2000, 2, 29)
    # 2027 is common: the birthday lands on the 28th rather than nowhere.
    assert birthday_offset(born, datetime.date(2027, 2, 28), within=0) == 0
    assert age_turning(born, datetime.date(2027, 2, 28), 0) == 27
    # 2028 is a leap year: the real day, and the 28th is NOT the birthday.
    assert birthday_offset(born, datetime.date(2028, 2, 29), within=0) == 0
    assert birthday_offset(born, datetime.date(2028, 2, 28), within=1) == 1


def test_the_digest_carries_who_when_and_which_age():
    today = datetime.date(2026, 6, 15)
    people = [
        ("Marko Marić", datetime.date(2014, 6, 16)),
        ("Ana Horvat", datetime.date(1992, 6, 15)),
        ("Marko Ivić", datetime.date(1972, 12, 1)),  # outside the window
    ]
    assert digest(people, today, within=1) == "0=Ana Horvat:34;1=Marko Marić:12"
    assert digest(people, today, within=0) == "0=Ana Horvat:34"
    assert digest([], today, within=1) == ""


def test_a_name_cannot_break_the_digest_apart():
    # The separators are the format; a name carrying one would split a field and
    # the rule reading it would speak a fragment.
    today = datetime.date(2026, 6, 15)
    assert digest([("Ana; Horvat:x=1", datetime.date(1992, 6, 15))], today, within=0) \
        == "0=Ana  Horvat x 1:34"
