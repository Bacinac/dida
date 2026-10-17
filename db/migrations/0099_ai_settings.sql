-- The AI provider's key and the model each role uses (Settings → AI), read and
-- written by home_core.llm.AiSettings. The key is Fernet-sealed with
-- DIDA_SECRET_KEY, as app_settings secrets are.
CREATE TABLE IF NOT EXISTS ai_settings (
    provider          TEXT PRIMARY KEY,
    api_key_encrypted BYTEA NOT NULL,
    enabled           BOOLEAN NOT NULL DEFAULT true,
    models            JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_used_at      TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO ai_settings (provider, api_key_encrypted, models)
SELECT 'anthropic', convert_to(value, 'UTF8'),
       '{"chat": "claude-sonnet-5-5", "author": "claude-opus-5-5"}'::jsonb
FROM app_settings WHERE key = 'anthropic_api_key' AND value <> ''
ON CONFLICT (provider) DO NOTHING;

DELETE FROM app_settings WHERE key IN ('anthropic_api_key', 'openai_api_key');

DELETE FROM translations WHERE key = 'assistant is not configured — add an Anthropic key in System → Settings';
INSERT INTO translations (key, lang, value) VALUES
  ('no AI provider is configured — open Settings → AI', 'hr',
   'nijedan AI pružatelj nije postavljen — otvori Postavke → Sustav → AI'),
  ('Anthropic is not configured — open Settings → AI', 'hr',
   'Anthropic nije postavljen — otvori Postavke → Sustav → AI'),
  ('no Anthropic model is chosen for chat — choose one in Settings → AI', 'hr',
   'za razgovor nije odabran Anthropic model — odaberi ga u Postavke → Sustav → AI'),
  ('no Anthropic model is chosen for author — choose one in Settings → AI', 'hr',
   'za pisanje pravila nije odabran Anthropic model — odaberi ga u Postavke → Sustav → AI'),
  ('the stored Anthropic key will not decrypt — enter it again in Settings → AI', 'hr',
   'spremljeni Anthropic ključ ne može se dešifrirati — upiši ga ponovno u Postavke → Sustav → AI'),
  ('the stored Anthropic key will not decrypt — enter it again', 'hr',
   'spremljeni Anthropic ključ ne može se dešifrirati — upiši ga ponovno'),
  ('Anthropic rejected the API key — replace it in Settings → AI', 'hr',
   'Anthropic je odbio API ključ — zamijeni ga u Postavke → Sustav → AI'),
  ('Anthropic rate limit reached — try again shortly', 'hr',
   'Dosegnuto je Anthropicovo ograničenje zahtjeva — pokušaj ponovno za koji trenutak'),
  ('an API key is required for a provider not yet configured', 'hr',
   'za pružatelja koji još nije postavljen potreban je API ključ'),
  ('provider not configured', 'hr', 'pružatelj nije postavljen'),
  ('no stored key; pass api_key', 'hr', 'ključ nije spremljen; upiši ga'),
  ('the automation service is not answering', 'hr', 'servis automatizacija ne odgovara'),
  ('OPUS answers', 'hr', 'OPUS odgovara'),
  ('OPUS does not answer', 'hr', 'OPUS ne odgovara'),
  ('OPUS rejected the token', 'hr', 'OPUS je odbio token'),
  ('no OPUS address entered', 'hr', 'adresa OPUS-a nije upisana'),
  ('no OPUS token entered', 'hr', 'token za OPUS nije upisan'),
  ('no location received yet', 'hr', 'nijedna lokacija još nije primljena')
ON CONFLICT (key, lang) DO UPDATE SET value = EXCLUDED.value, updated_at = now();
