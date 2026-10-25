Grijanje je regulacijska petlja, a ne pravilo. Ventil radijatora u svakoj prostoriji otvara i zatvara njezin termometar, a relej kotla — običan termostatski kontakt — uključen je dok god je barem jednoj prostoriji koja smije paliti kotao hladnije od cilja.

## Prostorije

- Svaka prostorija ima senzor temperature, svoje ventile, četiri zadane temperature (Komfor, Eko, Noć, Odsutnost) i tjedni raspored koji kaže koja kada vrijedi. Bez rasporeda prostorija stalno drži Komfor.
- **±** na kartici prostorije drži je sat vremena na toj temperaturi, a zatim se vraća rasporedu.
- Otvoren prozor pauzira prostoriju: prema kontaktnom senzoru ako ga ima, inače prema DIDA-inu vlastitom očitanju da temperatura pada brže nego što grijanje može objasniti.
- **Eko kad nikoga nema** (prostorija sa senzorom prisutnosti ili pokreta): kad nijedan senzor u prostoriji nikoga ne vidi zadani broj minuta, Komfor ide na Eko; čim netko uđe, vraća se Komfor. Nedostupan senzor ili senzor koji se nikad nije javio drži prostoriju na Komforu. I jutarnje zagrijavanje prazne prostorije ide prema Eku.
- U prostoriji sa svojim termometrom ventili se samo otvaraju i zatvaraju: DIDA ih drži otvorenima dok prostorija traži toplinu, inače zatvorenima. Termometar u ventilu je uz radijator i ne koristi se. Prostorija bez termometra, isključena prostorija, otvoren prozor i zaštita od smrzavanja ostavljaju ventil da regulira sam, pa zaštita od smrzavanja ne ovisi o tome radi li DIDA.

## Kuća

- **Način rada kuće** nadjačava sve prostorije odjednom; Automatski pušta svaku prostoriju da slijedi svoj raspored. Prazna kuća (podešeni helper) sve prebacuje na Odsutnost kad potraje koliko i prazna soba, pa jedna zalutala lokacija mobitela ne hladi kuću.
- **Vanjski senzor** radi tri stvari: iznad ljetne granice zaustavlja sezonu; jutarnje zagrijavanje započinje dovoljno rano da prostorija DOSEGNE zadanu temperaturu u predviđeno vrijeme, a ne da se tek tada počne grijati; a kad zahladi, podiže granicu protusmrzavanja.

## Kotao

- Najkraće vrijeme rada i najkraći odmor sprječavaju prečesto paljenje i gašenje.
- Prostoriji se može zabraniti da pali kotao: tada uzima toplinu samo dok kotao radi za druge i ne drži ga upaljenim. Kuća može tražiti da nekoliko prostorija istodobno traži toplinu prije nego što se kotao upali.

## Što odbija raditi

- Prostorija se ne grije prema očitanju koje je prestalo stizati.
- Ventil koji nikad ne potvrdi zadanu temperaturu ili otvaranje i zatvaranje prijavljuje se kao neispravan, umjesto da se tiho zanemari.
- Kotao se zbog zatvorenih ventila gasi samo kad je svaki ventil barem jednom viđen otvoren i sad javlja da je zatvoren; ventil koji uvijek javlja 0 % ništa ne dokazuje.
- Ništa se ne pokreće dok se grijanje ne uključi. Glavni prekidač omogućuje da se kuća sigurno podesi prije početka sezone.
