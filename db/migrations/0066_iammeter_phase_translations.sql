-- Croatian for the per-phase descriptors a three-phase IAMMETER (WEM3080T) emits
-- alongside the totals. Same rule as 0023: the key IS the English descriptor.
INSERT INTO translations (key, lang, value) VALUES
    ('L1 voltage',      'hr', 'L1 napon'),
    ('L2 voltage',      'hr', 'L2 napon'),
    ('L3 voltage',      'hr', 'L3 napon'),
    ('L1 current',      'hr', 'L1 struja'),
    ('L2 current',      'hr', 'L2 struja'),
    ('L3 current',      'hr', 'L3 struja'),
    ('L1 power',        'hr', 'L1 snaga'),
    ('L2 power',        'hr', 'L2 snaga'),
    ('L3 power',        'hr', 'L3 snaga'),
    ('L1 power factor', 'hr', 'L1 faktor snage'),
    ('L2 power factor', 'hr', 'L2 faktor snage'),
    ('L3 power factor', 'hr', 'L3 faktor snage')
ON CONFLICT (key, lang) DO NOTHING;
