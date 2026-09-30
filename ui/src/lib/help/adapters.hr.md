Jedan adapter po protokolu, svaki u svojem procesu. Ako adapter padne, njegovi entiteti postanu nedostupni, a sve ostalo radi dalje; sustav ga ponovno pokreće.

DIDA ne piše protokole iznova — koristi mostove koji ih već dobro rade (zigbee2mqtt, ESPHome, Matter i druge).

Adapteri se podešavaju u Postavke → Adapteri, gdje svaki javlja svoje stanje. Adapter koji nije zdrav kaže zašto — neispravna vjerodajnica to i kaže, umjesto da adapter samo šuti (vidi [Sustav i podaci](/help/system)). Tajne unesene ondje spremaju se šifrirane i nikad se ne prikazuju natrag.
