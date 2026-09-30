[Pravilo](/help/automations) šalje komandu `notify` na `notify:<osoba>` (ili `notify:all`). Adapter odlučuje kako obavijest stvarno stiže na mobitel — web pushom u DIDA aplikaciju ili preko ntfy teme. Pravilo ne zna koje od toga, i ne treba znati.

- **Web push** traži da je aplikacija instalirana i da je dopuštenje dano jednom po uređaju, pod **Računom**.
- **Android prateća aplikacija** isto je sučelje uz GPS u pozadini, koji hrani [prisutnost](/help/presence) i zone. Sama se ažurira.

Obavijest nije jedini način da vam se nešto javi. `announce` umjesto toga izgovara poruku na zvučniku ([Mediji](/help/media)) — to pravilo obično treba raditi danju, a nikad tijekom tihih sati.
