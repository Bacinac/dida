# SmartThings

For Samsung appliances and AV equipment that talk only to the SmartThings cloud:
washers, dryers, dishwashers, ovens, air conditioners, soundbars. The adapter
reads and controls them through the SmartThings API and polls every 30 seconds
(Poll interval). Zigbee devices are better served by DIDA's own Zigbee adapter
(Zigbee2MQTT), and a Samsung TV on the LAN by the `samsungtv` adapter, which
needs no cloud.

SmartThings personal access tokens created since 30 December 2024 expire after
24 hours, so DIDA connects through an OAuth app of your own, whose refresh token
renews itself. The app is created once.

Samsung has announced a paid SmartThings API plan for individuals, $4.99 a
month, targeted for October 2026
([announcement](https://community.smartthings.com/t/a-new-enhanced-smartthings-api-experience/309947)).
Until it takes effect the API is free. Once it does, an account without the plan
may lose API access, and this adapter with it.

## 1. Pick the redirect URI

After you sign in, SmartThings sends the browser back to DIDA, and it accepts
only an https address or `http://localhost` for that.

- **DIDA has an https address** (Settings → Network → Public address): the
  redirect URI is `https://<that address>/api/smartthings/callback`, and DIDA
  fills it in itself.
- **Otherwise** use `http://localhost:5273/api/smartthings/callback`, enter it
  in the Redirect URI field in step 3, and do step 3 in a browser on the DIDA
  host, or through a tunnel from your own machine:
  `ssh -L 5273:localhost:5273 <dida-host>`, then open `http://localhost:5273`.

## 2. Create the OAuth app

At [account.smartthings.com/tokens](https://account.smartthings.com/tokens),
create a personal access token with everything under **Apps** ticked. It is
needed only for the call below and can be deleted afterwards; the app does not
depend on it.

```bash
TOKEN=<the personal access token>
REDIRECT=<the redirect URI from step 1>
curl -s https://api.smartthings.com/v1/apps \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d @- <<EOF
{
  "appName": "dida-$(openssl rand -hex 6)",
  "displayName": "DIDA",
  "description": "DIDA home automation",
  "appType": "API_ONLY",
  "classifications": ["CONNECTED_SERVICE"],
  "singleInstance": true,
  "principalType": "LOCATION",
  "oauth": {
    "clientName": "DIDA",
    "scope": ["r:devices:*", "x:devices:*", "r:locations:*"],
    "redirectUris": ["$REDIRECT"]
  }
}
EOF
```

The answer holds `oauthClientId` and `oauthClientSecret`. Keep the secret:
SmartThings does not show it again. A 403 means the token lacks the Apps
permissions.

The SmartThings CLI creates the same app interactively: `smartthings
apps:create`, OAuth-In App, the three scopes above and the same redirect URI.

## 3. Connect

In DIDA, Settings → Adapters → SmartThings: switch the adapter on, enter the
Client ID and Client Secret (and the Redirect URI, if DIDA did not fill it in),
save, then **Connect SmartThings**. Sign in to Samsung and allow access; the
browser comes back to DIDA and the devices already in your SmartThings app
appear within one poll. If the account spans more than one home, pick this one
under Location.

The adapter renews the refresh token daily. It dies only if DIDA stays off for
30 days or the access is revoked at Samsung; the adapter then reports that it
needs to be connected again, and Disconnect followed by Connect restores it.
