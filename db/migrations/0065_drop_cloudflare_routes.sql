-- The ingress goes back to a file — but a different one, and for the opposite reason
-- than 0063 assumed.
--
-- 0063 moved routes into Postgres to stop config.yml being hand-edited. What it could
-- not see is that config.yml is only HALF the ingress: the same routing also drives the
-- Caddyfile (LAN, in another container), the router's split-DNS and the Homepage tiles,
-- and those are rendered from /mnt/docker/main/ingress/services.conf. So the DB was
-- never the single source — it was a second one, and it knew nothing about the LAN side.
-- A route edited here silently reverted whatever the renderer had produced.
--
-- services.conf is the strict superset, so it wins. The adapter now edits that file and a
-- systemd path unit on the Proxmox host applies it — the only place that can reach Caddy,
-- the connectors, Cloudflare, the router and Homepage at once.

DROP INDEX IF EXISTS cloudflare_routes_position_idx;
DROP TABLE IF EXISTS cloudflare_routes;
DROP TABLE IF EXISTS cloudflare_config;

-- The adapter's config fields changed with it: config.yml, the origin cert and the
-- connector list are the host applier's business now.
DELETE FROM adapter_config
 WHERE adapter = 'cloudflare'
   AND key IN ('config_path', 'origin_cert', 'containers', 'reload_wait_s');
