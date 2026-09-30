"""The UniFi people map and client matching.

Presence for the whole household hangs off these two functions: a person mapped to
the wrong client is silently reported home while they are away, which is worse than
no presence at all.
"""
from dida_adapter_unifi.adapter import UnifiAdapter, _parse_people

CLIENTS = [
    {"name": "Mobile Marko", "macAddress": "BA:00:53:00:02:29", "type": "WIRELESS",
     "uplinkDeviceId": "ap-down"},
    {"name": "Mobile Ana", "macAddress": "92:00:53:00:AC:D6", "type": "WIRELESS",
     "uplinkDeviceId": "ap-up"},
    {"name": "Speaker Kitchen", "macAddress": "AA:BB:CC:DD:EE:FF", "type": "WIRELESS",
     "uplinkDeviceId": "ap-north"},
]


def test_parses_a_person_per_line():
    people = _parse_people("marko: Mobile Marko\n\n# komentar\nana: Mobile Ana | 92:00:53:00:ac:d6\n")
    assert [p["user"] for p in people] == ["marko", "ana"]
    assert people[0]["name"] == "Marko" and people[0]["macs"] == set()
    assert people[1]["macs"] == {"92:00:53:00:ac:d6"}


def test_ignores_a_line_without_a_client_name():
    assert _parse_people("marko:\n: Mobile Marko\nrubbish\n") == []


def test_falls_back_to_the_name_when_no_mac_is_configured():
    person = _parse_people("marko: mobile marko")[0]
    assert UnifiAdapter._match(CLIENTS, person)["macAddress"] == "BA:00:53:00:02:29"


def test_a_configured_mac_identifies_the_device_whatever_it_is_called():
    # The controller's client name is editable and not unique; the MAC is what the
    # household's phones have actually held for years.
    person = _parse_people("marko: whatever | ba:00:53:00:02:29")[0]
    assert UnifiAdapter._match(CLIENTS, person)["name"] == "Mobile Marko"


def test_absent_client_is_no_match():
    person = _parse_people("mia: Mobile Mia")[0]
    assert UnifiAdapter._match(CLIENTS, person) is None


def test_mac_picks_one_of_two_clients_sharing_a_name():
    # Two clients are genuinely called "Mobile Marko"; a name alone cannot say which.
    clients = [*CLIENTS, {"name": "Mobile Marko", "macAddress": "22:00:53:00:F3:29",
                          "type": "WIRELESS", "uplinkDeviceId": "ap-up"}]
    person = _parse_people("marko: Mobile Marko | 22:00:53:00:f3:29")[0]
    assert UnifiAdapter._match(clients, person)["macAddress"] == "22:00:53:00:F3:29"

    absent = _parse_people("marko: Mobile Marko | 00:00:00:00:00:01")[0]
    assert UnifiAdapter._match(clients, absent) is None


def test_match_carries_the_access_point_so_the_zone_is_known():
    person = _parse_people("ana: Mobile Ana")[0]
    assert UnifiAdapter._match(CLIENTS, person)["uplinkDeviceId"] == "ap-up"
