Jedna stranica za sve što proizvodi zvuk. Na vrhu izbor prostora (prostorije s podešenim izvorom), ispod njega izbor izvora, a sadržaj se mijenja prema onome što svira.

- Playeri su obični entiteti s medijskim sposobnostima (reprodukcija, glasnoća, izvor), pa njima automatizacija ili asistent upravljaju kao i svakim drugim uređajem.
- Sama glazba nije DIDA-ina. Albumi i radijske postaje žive u **OPUS · Playeru**, koji ova stranica pita na licu mjesta. Odabir albuma ili postaje stavlja ga na player tog prostora, a uređaj zvuk preuzima izravno iz OPUS-a.

## Posebni playeri

- `radio:tuner` — sintetički entitet kojim automatizacije i daljinski upravljači prolaze kroz postaje (sljedeća, prethodna) ili po imenu postave jednu na podešeni player.
- `opus:tv` — OPUS aplikacija na televizoru u dnevnom boravku, kao player: što reproducira (film, epizodu, pjesmu ili postaju), njezine tipke te albumi i postaje stavljeni na nju. Naredbe prima samo dok je OPUS otvoren na televizoru.
- `opus:dac` — DAC na OPUS poslužitelju, čiji zvuk ide na ulaz MUSIC pojačala: glazba i radio kuće (na njemu svira `radio:tuner`). Njegova oznaka kvalitete otvara put signala — datoteku kako ju je knjižnica izmjerila, naspram formata koji DAC prima.

## Govor nije reprodukcija

Govoriti kući nije isto što i puštati joj glazbu. Entitet `announce:<zvučnik>` prima komandu `say` s tekstom i ništa trajno ne prekida.
