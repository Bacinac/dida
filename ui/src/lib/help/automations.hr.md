Pravilo je **okidač → uvjeti → akcije**.

- **Okidač** — jedan entitet i sposobnost, po želji vrijednost NA koju se mijenja i po želji zadržavanje (`for_seconds`): pravilo se pokreće tek kad se vrijednost toliko dugo zadrži. Okidač reagira na PROMJENU vrijednosti, a ne na svako javljanje uređaja.
- **Uvjeti** — popis uvjeta koji svi moraju vrijediti (I). Bez uvjeta pravilo je bezuvjetno.
- **Akcije** — izvode se redom. Svaka može imati odgodu PRIJE sebe, i tako se grade nizovi i impulsi: relej vrata je klik, čekanje, klik.

## Kad obično pravilo nije dovoljno

Drugi sloj je **Starlark**: zaštićena skripta za logiku koju obično pravilo ne može izraziti, primjerice grananje ili čitanje više senzora odjednom. Nema pristup vanjskom svijetu ni beskonačne petlje, pa pravilo ne može zaglaviti ni srušiti jezgru. Svaka se skripta prije spremanja prevede i isproba.

## U uređivaču

- Pravilo se može skicirati iz opisa običnim riječima.
- Postojeće pravilo može se objasniti — uključujući bi li se pokrenulo upravo sada ili koji ga uvjet trenutačno blokira.
- Pravila se mogu uključiti, isključiti i prisilno pokrenuti. Prisilno pokretanje preskače okidač i uvjete i stvarno upravlja uređajima.

Svako pravilo bilježi svoja pokretanja i grešku kad pokretanje ne uspije ([Povijest i komande](/help/history)). Vrijednosti koje dijeli cijela kuća, poput tihih sati, pripadaju [helperu](/help/helpers), a ne uvjetu ponovljenom u svakom pravilu; datumi koji se ponavljaju pripadaju [rasporedu](/help/schedules).
