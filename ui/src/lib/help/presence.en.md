Each tracked person is an entity named `presence:<name>`. Its `location` reading is the name of the zone they are in, or `away` when they are in none. Some also report battery and raw coordinates.

The position comes from the phone: the Android companion app sends GPS in the background ([Notifications](/help/notifications)).

Zones are geofences — home, work, school — not rooms: a zone is matched against a phone's GPS position and has nothing to do with the floor plan ([Floor plans, rooms and zones](/help/space)).

People are not the same as login [users](/help/users): a person is someone the house tracks, a user is an account that can sign in.
