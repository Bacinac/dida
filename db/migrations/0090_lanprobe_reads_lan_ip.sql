-- lanprobe reports the interface that holds the configured LAN address, so it reads
-- that address; guessing it from the default route picked the IoT VLAN on a
-- multi-homed host.
DROP POLICY IF EXISTS host_service_read ON app_settings;
CREATE POLICY host_service_read ON app_settings FOR SELECT USING (
    (current_user = 'dida_netmgr' AND key IN ('managed_vlans', 'vlan_config', 'net_parent', 'vlan_status'))
    OR (current_user = 'dida_lanprobe' AND key IN ('lan_status', 'lan_ip'))
);
