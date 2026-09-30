-- netmgr and lanprobe must work with the bus down, so they keep the database, but
-- each as its own role that reaches only its own app_settings keys. The roles are
-- named, not granted to, here: the keys service creates them and the table is
-- granted by dida_core.db_roles. The owner every other service connects as is not
-- subject to row security.
ALTER TABLE app_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS host_service_read ON app_settings;
CREATE POLICY host_service_read ON app_settings FOR SELECT USING (
    (current_user = 'dida_netmgr' AND key IN ('managed_vlans', 'vlan_config', 'net_parent', 'vlan_status'))
    OR (current_user = 'dida_lanprobe' AND key = 'lan_status')
);

DROP POLICY IF EXISTS host_service_insert ON app_settings;
CREATE POLICY host_service_insert ON app_settings FOR INSERT WITH CHECK (
    (current_user = 'dida_netmgr' AND key = 'vlan_status')
    OR (current_user = 'dida_lanprobe' AND key = 'lan_status')
);

DROP POLICY IF EXISTS host_service_update ON app_settings;
CREATE POLICY host_service_update ON app_settings FOR UPDATE USING (
    (current_user = 'dida_netmgr' AND key = 'vlan_status')
    OR (current_user = 'dida_lanprobe' AND key = 'lan_status')
) WITH CHECK (
    (current_user = 'dida_netmgr' AND key = 'vlan_status')
    OR (current_user = 'dida_lanprobe' AND key = 'lan_status')
);
